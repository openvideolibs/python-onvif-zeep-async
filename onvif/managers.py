"""ONVIF Managers."""
from __future__ import annotations

from abc import abstractmethod
import asyncio
import datetime as dt
import logging
from typing import TYPE_CHECKING, Any, Callable

import httpx
from httpx import TransportError
from zeep.exceptions import Fault, XMLParseError, XMLSyntaxError
from zeep.loader import parse_xml
from zeep.wsdl.bindings.soap import SoapOperation

from onvif.exceptions import ONVIFError

from .settings import DEFAULT_SETTINGS
from .transport import ASYNC_TRANSPORT
from .util import normalize_url, stringify_onvif_error
from .wrappers import retry_connection_error

logger = logging.getLogger("onvif")


_RENEWAL_PERCENTAGE = 0.8

SUBSCRIPTION_ERRORS = (Fault, asyncio.TimeoutError, TransportError)
RENEW_ERRORS = (ONVIFError, httpx.RequestError, XMLParseError, *SUBSCRIPTION_ERRORS)
SUBSCRIPTION_RESTART_INTERVAL_ON_ERROR = dt.timedelta(seconds=40)


if TYPE_CHECKING:
    from onvif.client import ONVIFCamera, ONVIFService


class BaseManager:
    """Base class for notification and pull point managers."""

    def __init__(
        self,
        device: ONVIFCamera,
        interval: dt.timedelta,
        subscription_lost_callback: Callable[[], None],
    ) -> None:
        """Initialize the notification processor."""
        self._device = device
        self._interval = interval
        self._subscription: ONVIFService | None = None
        self._restart_or_renew_task: asyncio.Task | None = None
        self._loop = asyncio.get_event_loop()
        self._shutdown = False
        self._subscription_lost_callback = subscription_lost_callback
        self._cancel_subscription_renew: asyncio.TimerHandle | None = None
        self._service: ONVIFService | None = None

    @property
    def closed(self) -> bool:
        """Return True if the manager is closed."""
        return not self._subscription or self._subscription.transport.client.is_closed

    async def start(self) -> None:
        """Setup the manager."""
        renewal_call_at = await self._start()
        self._schedule_subscription_renew(renewal_call_at)
        return self._subscription

    def pause(self) -> None:
        """Pause the manager."""
        self._cancel_renewals()

    def resume(self) -> None:
        """Resume the manager."""
        self._schedule_subscription_renew(self._loop.time())

    async def stop(self) -> None:
        """Stop the manager."""
        logger.debug("%s: Stop the notification manager", self._device.host)
        self._cancel_renewals()
        assert self._subscription, "Call start first"
        await self._subscription.Unsubscribe()

    async def shutdown(self) -> None:
        """Shutdown the manager.

        This method is irreversible.
        """
        self._shutdown = True
        if self._restart_or_renew_task:
            self._restart_or_renew_task.cancel()
        logger.debug("%s: Shutdown the notification manager", self._device.host)
        await self.stop()

    @abstractmethod
    async def _start(self) -> float:
        """Setup the processor. Returns the next renewal call at time."""

    async def set_synchronization_point(self) -> float:
        """Set the synchronization point."""
        try:
            await self._service.SetSynchronizationPoint()
        except (Fault, asyncio.TimeoutError, TransportError, TypeError):
            logger.debug("%s: SetSynchronizationPoint failed", self._service.url)

    def _cancel_renewals(self) -> None:
        """Cancel any pending renewals."""
        if self._cancel_subscription_renew:
            self._cancel_subscription_renew.cancel()
            self._cancel_subscription_renew = None

    def _calculate_next_renewal_call_at(self, result: Any | None) -> float:
        """Calculate the next renewal call_at."""
        current_time: dt.datetime | None = result.CurrentTime
        termination_time: dt.datetime | None = result.TerminationTime
        if termination_time and current_time:
            delay = termination_time - current_time
        else:
            delay = self._interval
        delay_seconds = delay.total_seconds() * _RENEWAL_PERCENTAGE
        logger.debug(
            "%s: Renew notification subscription in %s seconds",
            self._device.host,
            delay_seconds,
        )
        return self._loop.time() + delay_seconds

    def _schedule_subscription_renew(self, when: float) -> None:
        """Schedule notify subscription renewal."""
        self._cancel_renewals()
        self._cancel_subscription_renew = self._loop.call_at(
            when,
            self._run_restart_or_renew,
        )

    def _run_restart_or_renew(self) -> None:
        """Create a background task."""
        if self._restart_or_renew_task and not self._restart_or_renew_task.done():
            logger.debug("%s: Notify renew already in progress", self._device.host)
            return
        self._restart_or_renew_task = asyncio.create_task(
            self._renew_or_restart_subscription()
        )

    async def _restart_subscription(self) -> bool:
        """Restart the notify subscription assuming the camera rebooted."""
        self._cancel_renewals()
        return await self._start()

    @retry_connection_error()
    async def _call_subscription_renew(self) -> float:
        """Call notify subscription Renew."""
        device = self._device
        logger.debug("%s: Renew the notification manager", device.host)
        return self._calculate_next_renewal_call_at(
            await self._subscription.Renew(
                device.get_next_termination_time(self._interval)
            )
        )

    async def _renew_subscription(self) -> float | None:
        """Renew notify subscription."""
        if self.closed or self._shutdown:
            return None
        try:
            return await self._call_subscription_renew()
        except RENEW_ERRORS as err:
            self._subscription_lost_callback()
            logger.debug(
                "%s: Failed to renew notify subscription %s",
                self._device.host,
                stringify_onvif_error(err),
            )
        return None

    async def _renew_or_restart_subscription(self) -> None:
        """Renew or start notify subscription."""
        if self._shutdown:
            return
        renewal_call_at = None
        try:
            renewal_call_at = (
                await self._renew_subscription() or await self._restart_subscription()
            )
        finally:
            self._schedule_subscription_renew(
                renewal_call_at
                or self._loop.time()
                + SUBSCRIPTION_RESTART_INTERVAL_ON_ERROR.total_seconds()
            )


class NotificationManager(BaseManager):
    """Manager to process notifications."""

    def __init__(
        self,
        device: ONVIFCamera,
        address: str,
        interval: dt.timedelta,
        subscription_lost_callback: Callable[[], None],
    ) -> None:
        """Initialize the notification processor."""
        self._address = address
        self._operation: SoapOperation | None = None
        super().__init__(device, interval, subscription_lost_callback)

    async def _start(self) -> float:
        """Start the notification processor.

        Returns the next renewal call at time.
        """
        device = self._device
        logger.debug("%s: Setup the notification manager", device.host)
        notify_service = await device.create_notification_service()
        time_str = device.get_next_termination_time(self._interval)
        result = await notify_service.Subscribe(
            {
                "InitialTerminationTime": time_str,
                "ConsumerReference": {"Address": self._address},
            }
        )
        # pylint: disable=protected-access
        device.xaddrs[
            "http://www.onvif.org/ver10/events/wsdl/NotificationConsumer"
        ] = normalize_url(result.SubscriptionReference.Address._value_1)
        # Create subscription manager
        # 5.2.3 BASIC NOTIFICATION INTERFACE - NOTIFY
        # Call SetSynchronizationPoint to generate a notification message
        # to ensure the webhooks are working.
        #
        # If this fails this is OK as it just means we will switch
        # to webhook later when the first notification is received.
        self._service = await self._device.create_onvif_service(
            "pullpoint", port_type="NotificationConsumer"
        )
        self._operation = self._service.document.bindings[
            self._service.binding_name
        ].get("PullMessages")
        self._subscription = await device.create_subscription_service(
            "NotificationConsumer"
        )
        if device.has_broken_relative_time(
            self._interval,
            result.CurrentTime,
            result.TerminationTime,
        ):
            # If we determine the device has broken relative timestamps, we switch
            # to using absolute timestamps and renew the subscription.
            result = await self._subscription.Renew(
                device.get_next_termination_time(self._interval)
            )
        renewal_call_at = self._calculate_next_renewal_call_at(result)
        logger.debug("%s: Start the notification manager", self._device.host)
        return renewal_call_at

    def process(self, content: bytes) -> Any | None:
        """Process a notification message."""
        if not self._operation:
            logger.debug("%s: Notifications not setup", self._device.host)
            return
        try:
            envelope = parse_xml(
                content,  # type: ignore[arg-type]
                ASYNC_TRANSPORT,
                settings=DEFAULT_SETTINGS,
            )
        except XMLSyntaxError as exc:
            logger.error("Received invalid XML: %s", exc)
            return None
        return self._operation.process_reply(envelope)


class PullPointManager(BaseManager):
    """Manager for PullPoint."""

    async def _start(self) -> float:
        """Start the PullPoint manager.

        Returns the next renewal call at time.
        """
        device = self._device
        logger.debug("%s: Setup the PullPoint manager", device.host)
        events_service = await device.create_events_service()
        result = await events_service.CreatePullPointSubscription(
            {
                "InitialTerminationTime": device.get_next_termination_time(
                    self._interval
                ),
            }
        )
        # pylint: disable=protected-access
        device.xaddrs[
            "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription"
        ] = normalize_url(result.SubscriptionReference.Address._value_1)
        # Create subscription manager
        self._subscription = await device.create_subscription_service(
            "PullPointSubscription"
        )
        # Create the service that will be used to pull messages from the device.
        self._service = await device.create_pullpoint_service()
        if device.has_broken_relative_time(
            self._interval, result.CurrentTime, result.TerminationTime
        ):
            # If we determine the device has broken relative timestamps, we switch
            # to using absolute timestamps and renew the subscription.
            result = await self._subscription.Renew(
                device.get_next_termination_time(self._interval)
            )
        renewal_call_at = self._calculate_next_renewal_call_at(result)
        logger.debug("%s: Start the notification manager", self._device.host)
        return renewal_call_at

    def get_service(self) -> ONVIFService:
        """Return the pullpoint service."""
        return self._service
