"""Unit tests for :mod:`onvif.managers`.

These exercise the notification/pull-point subscription managers in isolation:
the :class:`~onvif.client.ONVIFCamera` device and the zeep-backed
:class:`~onvif.client.ONVIFService` subscriptions are mocked, so the tests
drive the managers' own logic -- renewal scheduling, the renew/restart fallback
chain, error handling and notification parsing -- without any network or WSDL.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import pytest
from zeep.exceptions import Fault, XMLSyntaxError

from onvif.managers import (
    SUBSCRIPTION_RESTART_INTERVAL_ON_ERROR,
    BaseManager,
    NotificationManager,
    PullPointManager,
)

INTERVAL = dt.timedelta(seconds=100)
# A reference address with the duplicated-port quirk some cameras emit, so the
# normalize_url() step actually changes the value (and the start tests can prove
# it ran). See onvif.util.normalize_url.
SUBSCRIPTION_ADDRESS = "http://192.168.0.10:8106:8106/onvif/Subscription?Idx=1"
NORMALIZED_ADDRESS = "http://192.168.0.10:8106/onvif/Subscription?Idx=1"


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------


class _ConcreteManager(BaseManager):
    """A minimal concrete BaseManager so the abstract base can be exercised."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.start_calls = 0
        self.start_result: float = 100.0
        self.start_subscription = None

    async def _start(self) -> float:
        self.start_calls += 1
        if self.start_subscription is not None:
            self._subscription = self.start_subscription
        return self.start_result


def _make_device(host: str = "1.2.3.4") -> Mock:
    """Build a mock ONVIFCamera with the helpers the managers call."""
    device = Mock()
    device.host = host
    device.xaddrs = {}
    device.get_next_termination_time = Mock(return_value="PT60S")
    device.has_broken_relative_time = Mock(return_value=False)
    # Managers run subscription addresses through device.rewrite_url so that
    # nat_override can swap the device-advertised host for the externally
    # routable one. The default mock is a no-op pass-through so existing tests
    # observe the original URL (matching nat_override=False).
    device.rewrite_url = Mock(side_effect=lambda url: url)
    return device


def _make_subscription(closed: bool = False) -> Mock:
    """Build a mock subscription service (the zeep SubscriptionManager)."""
    sub = Mock()
    sub.transport.session.closed = closed
    sub.Unsubscribe = AsyncMock()
    sub.Renew = AsyncMock()
    return sub


def _mock_loop(now: float = 1000.0) -> Mock:
    """A fake event loop with deterministic time and a no-op call_at."""
    loop = Mock()
    loop.time.return_value = now
    loop.call_at.return_value = Mock()  # stand-in TimerHandle
    return loop


def _renew_result(
    current: dt.datetime | None = None, termination: dt.datetime | None = None
) -> SimpleNamespace:
    """A Renew/Subscribe response carrying CurrentTime/TerminationTime."""
    return SimpleNamespace(CurrentTime=current, TerminationTime=termination)


def _subscribe_result(
    address: str = SUBSCRIPTION_ADDRESS,
    current: dt.datetime | None = None,
    termination: dt.datetime | None = None,
) -> SimpleNamespace:
    """A Subscribe/CreatePullPointSubscription response with a reference addr."""
    return SimpleNamespace(
        SubscriptionReference=SimpleNamespace(
            Address=SimpleNamespace(_value_1=address)
        ),
        CurrentTime=current,
        TerminationTime=termination,
    )


async def _make_base_manager(device: Mock | None = None) -> _ConcreteManager:
    """Construct a concrete BaseManager (must run inside an event loop)."""
    return _ConcreteManager(device or _make_device(), INTERVAL, Mock())


# --------------------------------------------------------------------------
# BaseManager: construction & simple accessors
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_init_sets_defaults() -> None:
    device = _make_device()
    callback = Mock()
    mgr = _ConcreteManager(device, INTERVAL, callback)

    assert mgr._device is device
    assert mgr._interval is INTERVAL
    assert mgr._subscription is None
    assert mgr._restart_or_renew_task is None
    assert mgr._shutdown is False
    assert mgr._subscription_lost_callback is callback
    assert mgr._cancel_subscription_renew is None
    assert mgr._service is None


@pytest.mark.asyncio
async def test_closed_when_no_subscription() -> None:
    mgr = await _make_base_manager()
    # No subscription has been established yet.
    assert mgr.closed is True


@pytest.mark.asyncio
async def test_closed_reflects_session_state() -> None:
    mgr = await _make_base_manager()

    mgr._subscription = _make_subscription(closed=False)
    assert mgr.closed is False

    mgr._subscription = _make_subscription(closed=True)
    assert mgr.closed is True


# --------------------------------------------------------------------------
# BaseManager: lifecycle (start/pause/resume/stop/shutdown)  # noqa: ERA001
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_runs_start_and_schedules_renewal() -> None:
    mgr = await _make_base_manager()
    mgr._loop = _mock_loop()
    subscription = _make_subscription()
    mgr.start_subscription = subscription
    mgr.start_result = 1234.0

    result = await mgr.start()

    assert result is subscription
    assert mgr.start_calls == 1
    mgr._loop.call_at.assert_called_once_with(1234.0, mgr._run_restart_or_renew)


@pytest.mark.asyncio
async def test_pause_cancels_renewals() -> None:
    mgr = await _make_base_manager()
    handle = Mock()
    mgr._cancel_subscription_renew = handle

    mgr.pause()

    handle.cancel.assert_called_once()
    assert mgr._cancel_subscription_renew is None


@pytest.mark.asyncio
async def test_resume_schedules_renewal_now() -> None:
    mgr = await _make_base_manager()
    mgr._loop = _mock_loop(now=500.0)

    mgr.resume()

    mgr._loop.call_at.assert_called_once_with(500.0, mgr._run_restart_or_renew)


@pytest.mark.asyncio
async def test_stop_cancels_and_unsubscribes() -> None:
    mgr = await _make_base_manager()
    handle = Mock()
    mgr._cancel_subscription_renew = handle
    subscription = _make_subscription()
    mgr._subscription = subscription

    await mgr.stop()

    handle.cancel.assert_called_once()
    subscription.Unsubscribe.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        Fault("camera gone"),
        asyncio.TimeoutError(),
        aiohttp.ClientError(),
    ],
)
async def test_stop_swallows_unsubscribe_errors(error: Exception) -> None:
    # Teardown is best-effort: an offline camera (the common reason a consumer
    # is tearing the manager down) must not make stop()/shutdown() raise. The
    # remote Unsubscribe is courtesy -- the subscription times out on the camera
    # side regardless.
    mgr = await _make_base_manager()
    handle = Mock()
    mgr._cancel_subscription_renew = handle
    subscription = _make_subscription()
    subscription.Unsubscribe = AsyncMock(side_effect=error)
    mgr._subscription = subscription

    await mgr.stop()  # must not raise

    handle.cancel.assert_called_once()
    subscription.Unsubscribe.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_completes_when_unsubscribe_fails() -> None:
    mgr = await _make_base_manager()
    task = Mock()
    mgr._restart_or_renew_task = task
    subscription = _make_subscription()
    subscription.Unsubscribe = AsyncMock(side_effect=aiohttp.ClientError())
    mgr._subscription = subscription

    await mgr.shutdown()  # must not raise even though the camera is unreachable

    assert mgr._shutdown is True
    task.cancel.assert_called_once()


@pytest.mark.asyncio
async def test_stop_without_subscription_raises() -> None:
    mgr = await _make_base_manager()
    # start() was never called, so there is no subscription to unsubscribe.
    with pytest.raises(AssertionError):
        await mgr.stop()


@pytest.mark.asyncio
async def test_shutdown_cancels_task_and_stops() -> None:
    mgr = await _make_base_manager()
    task = Mock()
    mgr._restart_or_renew_task = task
    subscription = _make_subscription()
    mgr._subscription = subscription

    await mgr.shutdown()

    assert mgr._shutdown is True
    task.cancel.assert_called_once()
    subscription.Unsubscribe.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_without_pending_task() -> None:
    mgr = await _make_base_manager()
    mgr._subscription = _make_subscription()

    await mgr.shutdown()

    assert mgr._shutdown is True


# --------------------------------------------------------------------------
# BaseManager: set_synchronization_point  # noqa: ERA001
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_synchronization_point_calls_service() -> None:
    mgr = await _make_base_manager()
    service = Mock()
    service.url = "http://camera/onvif"
    service.SetSynchronizationPoint = AsyncMock()
    mgr._service = service

    await mgr.set_synchronization_point()

    service.SetSynchronizationPoint.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [TimeoutError(), Fault("boom"), aiohttp.ClientError(), TypeError()],
)
async def test_set_synchronization_point_swallows_errors(error: Exception) -> None:
    mgr = await _make_base_manager()
    service = Mock()
    service.url = "http://camera/onvif"
    service.SetSynchronizationPoint = AsyncMock(side_effect=error)
    mgr._service = service

    # Must not propagate; the camera will fall back to webhooks otherwise.
    await mgr.set_synchronization_point()


# --------------------------------------------------------------------------
# BaseManager: renewal-time calculation & scheduling
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calculate_next_renewal_uses_termination_window() -> None:
    mgr = await _make_base_manager()
    mgr._loop = _mock_loop(now=1000.0)
    now = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
    # 120s window -> 120 * 0.8 = 96s ahead of the loop clock.
    result = _renew_result(current=now, termination=now + dt.timedelta(seconds=120))

    assert mgr._calculate_next_renewal_call_at(result) == 1096.0


@pytest.mark.asyncio
async def test_calculate_next_renewal_falls_back_to_interval() -> None:
    mgr = await _make_base_manager()
    mgr._loop = _mock_loop(now=1000.0)
    # No timestamps -> use the configured interval (100s) * 0.8 = 80s.
    result = _renew_result(current=None, termination=None)

    assert mgr._calculate_next_renewal_call_at(result) == 1080.0


@pytest.mark.asyncio
async def test_calculate_next_renewal_clamps_to_minimum() -> None:
    mgr = await _make_base_manager()
    mgr._loop = _mock_loop(now=1000.0)
    now = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
    # A tiny 10s window is clamped to the 60s minimum: 60 * 0.8 = 48s.
    result = _renew_result(current=now, termination=now + dt.timedelta(seconds=10))

    assert mgr._calculate_next_renewal_call_at(result) == 1048.0


@pytest.mark.asyncio
async def test_schedule_subscription_renew_cancels_previous() -> None:
    mgr = await _make_base_manager()
    mgr._loop = _mock_loop()
    previous = Mock()
    mgr._cancel_subscription_renew = previous

    mgr._schedule_subscription_renew(2000.0)

    previous.cancel.assert_called_once()
    mgr._loop.call_at.assert_called_once_with(2000.0, mgr._run_restart_or_renew)
    assert mgr._cancel_subscription_renew is mgr._loop.call_at.return_value


@pytest.mark.asyncio
async def test_cancel_renewals_without_pending_handle() -> None:
    mgr = await _make_base_manager()
    # No scheduled renewal -> a no-op that must not raise.
    mgr._cancel_renewals()
    assert mgr._cancel_subscription_renew is None


# --------------------------------------------------------------------------
# BaseManager: _run_restart_or_renew task management
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_restart_or_renew_creates_task() -> None:
    mgr = await _make_base_manager()
    mgr._renew_or_restart_subscription = AsyncMock()

    mgr._run_restart_or_renew()
    assert mgr._restart_or_renew_task is not None
    # Let the scheduled task run to completion.
    await asyncio.sleep(0)

    mgr._renew_or_restart_subscription.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_restart_or_renew_skips_when_already_running() -> None:
    mgr = await _make_base_manager()
    running = Mock()
    running.done.return_value = False
    mgr._restart_or_renew_task = running

    with patch("asyncio.create_task") as create_task:
        mgr._run_restart_or_renew()

    create_task.assert_not_called()
    assert mgr._restart_or_renew_task is running


# --------------------------------------------------------------------------
# BaseManager: restart / renew chain
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_subscription_cancels_and_restarts() -> None:
    mgr = await _make_base_manager()
    handle = Mock()
    mgr._cancel_subscription_renew = handle
    mgr.start_result = 4242.0

    result = await mgr._restart_subscription()

    assert result == 4242.0
    assert mgr.start_calls == 1
    handle.cancel.assert_called_once()


@pytest.mark.asyncio
async def test_call_subscription_renew_returns_calculated_time() -> None:
    device = _make_device()
    device.get_next_termination_time = Mock(return_value="PT60S")
    mgr = await _make_base_manager(device)
    mgr._loop = _mock_loop(now=1000.0)
    subscription = _make_subscription()
    subscription.Renew = AsyncMock(return_value=_renew_result())
    mgr._subscription = subscription

    result = await mgr._call_subscription_renew()

    # No timestamps in the response -> interval-based 80s ahead.
    assert result == 1080.0
    subscription.Renew.assert_awaited_once_with("PT60S")


@pytest.mark.asyncio
async def test_renew_subscription_returns_none_when_closed() -> None:
    mgr = await _make_base_manager()
    callback = mgr._subscription_lost_callback
    # closed because there is no subscription.
    assert await mgr._renew_subscription() is None
    callback.assert_not_called()


@pytest.mark.asyncio
async def test_renew_subscription_returns_none_when_shutdown() -> None:
    mgr = await _make_base_manager()
    mgr._subscription = _make_subscription(closed=False)
    mgr._shutdown = True

    assert await mgr._renew_subscription() is None


@pytest.mark.asyncio
async def test_renew_subscription_returns_renewal_time() -> None:
    mgr = await _make_base_manager()
    mgr._subscription = _make_subscription(closed=False)
    mgr._call_subscription_renew = AsyncMock(return_value=77.0)

    assert await mgr._renew_subscription() == 77.0


@pytest.mark.asyncio
async def test_renew_subscription_reports_lost_on_error() -> None:
    mgr = await _make_base_manager()
    mgr._subscription = _make_subscription(closed=False)
    mgr._call_subscription_renew = AsyncMock(side_effect=aiohttp.ClientError())

    assert await mgr._renew_subscription() is None
    mgr._subscription_lost_callback.assert_called_once()


@pytest.mark.asyncio
async def test_renew_or_restart_returns_early_when_shutdown() -> None:
    mgr = await _make_base_manager()
    mgr._shutdown = True
    mgr._schedule_subscription_renew = Mock()

    await mgr._renew_or_restart_subscription()

    mgr._schedule_subscription_renew.assert_not_called()


@pytest.mark.asyncio
async def test_renew_or_restart_returns_early_when_session_closed() -> None:
    """A closed session must end the renewal loop, not restart it.

    The consumer owns the aiohttp session; once it is closed (e.g. Home
    Assistant unloading the config entry) no renew or restart can ever
    succeed, so the task must bail out without re-arming the timer.
    """
    mgr = await _make_base_manager()
    mgr._subscription = _make_subscription(closed=True)
    mgr._renew_subscription = AsyncMock()
    mgr._restart_subscription = AsyncMock()
    mgr._schedule_subscription_renew = Mock()

    await mgr._renew_or_restart_subscription()

    mgr._renew_subscription.assert_not_awaited()
    mgr._restart_subscription.assert_not_awaited()
    mgr._schedule_subscription_renew.assert_not_called()


@pytest.mark.asyncio
async def test_renew_or_restart_stops_when_session_closes_mid_flight() -> None:
    """A session closed while a renewal is in flight must not re-arm the timer.

    Mirrors the consumer closing the session between the entry guard and the
    renew/restart round trips: the restart fails with a ClientError and the
    ``finally`` block must see the closed session and end the loop instead of
    rescheduling the error retry.
    """
    mgr = await _make_base_manager()
    subscription = _make_subscription(closed=False)
    mgr._subscription = subscription
    mgr._schedule_subscription_renew = Mock()

    async def _renew_with_session_closing() -> float | None:
        subscription.transport.session.closed = True
        return None

    mgr._renew_subscription = _renew_with_session_closing
    mgr._restart_subscription = AsyncMock(
        side_effect=aiohttp.ClientConnectionError("Session is closed")
    )

    await mgr._renew_or_restart_subscription()  # must not raise

    mgr._schedule_subscription_renew.assert_not_called()


@pytest.mark.asyncio
async def test_renew_or_restart_schedules_on_successful_renew() -> None:
    mgr = await _make_base_manager()
    mgr._renew_subscription = AsyncMock(return_value=123.0)
    mgr._restart_subscription = AsyncMock()
    mgr._schedule_subscription_renew = Mock()

    await mgr._renew_or_restart_subscription()

    mgr._schedule_subscription_renew.assert_called_once_with(123.0)
    mgr._restart_subscription.assert_not_awaited()


@pytest.mark.asyncio
async def test_renew_or_restart_restarts_when_renew_fails() -> None:
    mgr = await _make_base_manager()
    mgr._renew_subscription = AsyncMock(return_value=None)
    mgr._restart_subscription = AsyncMock(return_value=456.0)
    mgr._schedule_subscription_renew = Mock()

    await mgr._renew_or_restart_subscription()

    mgr._restart_subscription.assert_awaited_once()
    mgr._schedule_subscription_renew.assert_called_once_with(456.0)


@pytest.mark.asyncio
async def test_renew_or_restart_handles_restart_failure() -> None:
    """A failing restart must not escape the fire-and-forget task.

    `_renew_or_restart_subscription` runs as an unawaited asyncio.Task, so any
    exception escaping the body surfaces as "Task exception was never
    retrieved" in production logs. Renew already swallows RENEW_ERRORS; restart
    must do the same and let the finally branch schedule the error retry.
    """
    mgr = await _make_base_manager()
    mgr._loop = _mock_loop(now=1000.0)
    mgr._renew_subscription = AsyncMock(return_value=None)
    mgr._restart_subscription = AsyncMock(side_effect=aiohttp.ClientError())
    mgr._schedule_subscription_renew = Mock()

    await mgr._renew_or_restart_subscription()  # must not raise

    expected = 1000.0 + SUBSCRIPTION_RESTART_INTERVAL_ON_ERROR.total_seconds()
    mgr._schedule_subscription_renew.assert_called_once_with(expected)


@pytest.mark.asyncio
async def test_renew_or_restart_uses_error_interval_on_total_failure() -> None:
    mgr = await _make_base_manager()
    mgr._loop = _mock_loop(now=1000.0)
    mgr._renew_subscription = AsyncMock(return_value=None)
    mgr._restart_subscription = AsyncMock(return_value=None)
    mgr._schedule_subscription_renew = Mock()

    await mgr._renew_or_restart_subscription()

    expected = 1000.0 + SUBSCRIPTION_RESTART_INTERVAL_ON_ERROR.total_seconds()
    mgr._schedule_subscription_renew.assert_called_once_with(expected)


@pytest.mark.asyncio
async def test_renew_or_restart_does_not_reschedule_when_shutdown_mid_flight() -> None:
    """A shutdown mid-renewal must not re-arm the renewal timer.

    shutdown() sets ``_shutdown`` and then cancels this fire-and-forget task.
    The resulting CancelledError still runs the ``finally`` block, so without a
    guard the renewal timer is rescheduled *after* teardown -- contradicting the
    "irreversible" shutdown contract and leaking a live TimerHandle.
    """
    mgr = await _make_base_manager()
    mgr._schedule_subscription_renew = Mock()

    async def _renew_then_cancelled() -> float | None:
        # Mirror shutdown() cancelling the task while the renew await is in
        # flight: _shutdown is already set, then CancelledError propagates.
        mgr._shutdown = True
        raise asyncio.CancelledError

    mgr._renew_subscription = _renew_then_cancelled

    with pytest.raises(asyncio.CancelledError):
        await mgr._renew_or_restart_subscription()

    mgr._schedule_subscription_renew.assert_not_called()


@pytest.mark.asyncio
async def test_shutdown_leaves_no_renewal_armed_with_task_in_flight() -> None:
    """End-to-end: shutdown() with an in-flight renewal leaves no live timer.

    Runs against the real event loop so the cancelled task's ``finally`` block
    actually executes during shutdown's teardown.
    """
    mgr = await _make_base_manager()
    mgr._subscription = _make_subscription()

    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_renew() -> float | None:
        started.set()
        await release.wait()  # block until the task is cancelled
        return None

    mgr._renew_subscription = _slow_renew

    # Launch the fire-and-forget task the way the renewal timer would.
    mgr._run_restart_or_renew()
    await started.wait()
    assert mgr._restart_or_renew_task is not None

    await mgr.shutdown()
    # Let the cancelled task settle and run its finally block.
    await asyncio.sleep(0)

    assert mgr._restart_or_renew_task.cancelled()
    # No renewal timer must survive an irreversible shutdown.
    assert mgr._cancel_subscription_renew is None


# --------------------------------------------------------------------------
# NotificationManager
# --------------------------------------------------------------------------


def _make_notification_manager(
    device: Mock, broken_time: bool = False
) -> tuple[NotificationManager, Mock, Mock, Mock]:
    """Wire up a NotificationManager with mocked device service creation."""
    notify_service = Mock()
    notify_service.Subscribe = AsyncMock(return_value=_subscribe_result())
    device.create_notification_service = AsyncMock(return_value=notify_service)

    consumer_service = Mock()
    consumer_service.binding_name = "binding"
    operation = Mock()
    consumer_service.document.bindings = {"binding": Mock()}
    consumer_service.document.bindings["binding"].get = Mock(return_value=operation)
    device.create_onvif_service = AsyncMock(return_value=consumer_service)

    subscription = _make_subscription()
    subscription.Renew = AsyncMock(return_value=_renew_result())
    device.create_subscription_service = AsyncMock(return_value=subscription)
    device.has_broken_relative_time = Mock(return_value=broken_time)

    mgr = NotificationManager(device, "http://callback/notify", INTERVAL, Mock())
    return mgr, notify_service, subscription, operation


@pytest.mark.asyncio
async def test_notification_manager_start() -> None:
    device = _make_device()
    mgr, notify_service, subscription, operation = _make_notification_manager(device)

    renewal = await mgr._start()

    assert isinstance(renewal, float)
    assert mgr._subscription is subscription
    assert mgr._operation is operation
    consumer_key = "http://www.onvif.org/ver10/events/wsdl/NotificationConsumer"
    # The reference address was normalized (duplicate port stripped) before being
    # recorded, so the stored value differs from the raw response address.
    assert device.xaddrs[consumer_key] == NORMALIZED_ADDRESS
    assert device.xaddrs[consumer_key] != SUBSCRIPTION_ADDRESS
    notify_service.Subscribe.assert_awaited_once()
    # The consumer service and subscription are created with the documented
    # port types; pin those so a contract drift is caught.
    device.create_onvif_service.assert_awaited_once_with(
        "pullpoint", port_type="NotificationConsumer"
    )
    device.create_subscription_service.assert_awaited_once_with("NotificationConsumer")
    consumer_service = device.create_onvif_service.return_value
    consumer_service.document.bindings["binding"].get.assert_called_once_with(
        "PullMessages"
    )
    # Relative timestamps are fine, so no extra Renew round-trip.
    subscription.Renew.assert_not_awaited()


@pytest.mark.asyncio
async def test_notification_manager_start_renews_on_broken_relative_time() -> None:
    device = _make_device()
    mgr, _notify_service, subscription, _operation = _make_notification_manager(
        device, broken_time=True
    )

    await mgr._start()

    # Broken relative timestamps force a renew with an absolute termination time.
    subscription.Renew.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_returns_none_when_not_setup() -> None:
    device = _make_device()
    mgr = NotificationManager(device, "http://callback", INTERVAL, Mock())
    # _start was never called, so there is no PullMessages operation.
    assert mgr.process(b"<xml/>") is None


@pytest.mark.asyncio
async def test_process_parses_and_returns_reply() -> None:
    device = _make_device()
    mgr = NotificationManager(device, "http://callback", INTERVAL, Mock())
    operation = Mock()
    sentinel = object()
    operation.process_reply = Mock(return_value=sentinel)
    mgr._operation = operation

    envelope = object()
    with patch("onvif.managers.parse_xml", return_value=envelope) as parse_xml:
        result = mgr.process(b"<xml/>")

    assert result is sentinel
    parse_xml.assert_called_once()
    operation.process_reply.assert_called_once_with(envelope)


@pytest.mark.asyncio
async def test_process_retries_decoding_on_syntax_error() -> None:
    device = _make_device()
    mgr = NotificationManager(device, "http://callback", INTERVAL, Mock())
    operation = Mock()
    sentinel = object()
    operation.process_reply = Mock(return_value=sentinel)
    mgr._operation = operation

    envelope = object()
    with patch(
        "onvif.managers.parse_xml",
        side_effect=[XMLSyntaxError("bad"), envelope],
    ) as parse_xml:
        result = mgr.process(b"<bad\xffxml/>")

    # First parse fails, the utf-8 re-encode retry succeeds.
    assert result is sentinel
    assert parse_xml.call_count == 2
    operation.process_reply.assert_called_once_with(envelope)


@pytest.mark.asyncio
async def test_process_returns_none_on_repeated_syntax_error() -> None:
    device = _make_device()
    mgr = NotificationManager(device, "http://callback", INTERVAL, Mock())
    mgr._operation = Mock()

    with patch(
        "onvif.managers.parse_xml",
        side_effect=[XMLSyntaxError("bad"), XMLSyntaxError("still bad")],
    ):
        assert mgr.process(b"<bad\xffxml/>") is None


# --------------------------------------------------------------------------
# PullPointManager
# --------------------------------------------------------------------------


def _make_pullpoint_manager(
    device: Mock, broken_time: bool = False
) -> tuple[PullPointManager, Mock, Mock]:
    """Wire up a PullPointManager with mocked device service creation."""
    events_service = Mock()
    events_service.CreatePullPointSubscription = AsyncMock(
        return_value=_subscribe_result()
    )
    device.create_events_service = AsyncMock(return_value=events_service)

    subscription = _make_subscription()
    subscription.Renew = AsyncMock(return_value=_renew_result())
    device.create_subscription_service = AsyncMock(return_value=subscription)

    pullpoint_service = Mock()
    device.create_pullpoint_service = AsyncMock(return_value=pullpoint_service)
    device.has_broken_relative_time = Mock(return_value=broken_time)

    mgr = PullPointManager(device, INTERVAL, Mock())
    return mgr, subscription, pullpoint_service


@pytest.mark.asyncio
async def test_pullpoint_manager_start() -> None:
    device = _make_device()
    mgr, subscription, pullpoint_service = _make_pullpoint_manager(device)

    renewal = await mgr._start()

    assert isinstance(renewal, float)
    assert mgr._subscription is subscription
    assert mgr._service is pullpoint_service
    pullpoint_key = "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription"
    # The reference address was normalized (duplicate port stripped) before being
    # recorded, so the stored value differs from the raw response address.
    assert device.xaddrs[pullpoint_key] == NORMALIZED_ADDRESS
    assert device.xaddrs[pullpoint_key] != SUBSCRIPTION_ADDRESS
    device.create_subscription_service.assert_awaited_once_with("PullPointSubscription")
    device.create_pullpoint_service.assert_awaited_once()
    subscription.Renew.assert_not_awaited()


@pytest.mark.asyncio
async def test_pullpoint_manager_start_renews_on_broken_relative_time() -> None:
    device = _make_device()
    mgr, subscription, _pullpoint_service = _make_pullpoint_manager(
        device, broken_time=True
    )

    await mgr._start()

    subscription.Renew.assert_awaited_once()


@pytest.mark.asyncio
async def test_pullpoint_manager_start_rewrites_subscription_address_on_nat_override() -> (
    None
):
    """Subscription addresses flow through device.rewrite_url for NAT support.

    When the device runs behind NAT it advertises the LAN address of the
    subscription endpoint; nat_override on ONVIFCamera replaces that with the
    external host:port the caller connected on. PullPointManager must defer
    that rewrite to the device so the stored xaddr is externally reachable.
    """
    device = _make_device()
    device.rewrite_url = Mock(return_value="http://wan.example.com:9000/rewritten")
    mgr, _subscription, _pullpoint_service = _make_pullpoint_manager(device)

    await mgr._start()

    pullpoint_key = "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription"
    # The manager handed the normalized address to rewrite_url and stored the
    # rewritten value; if the rewrite hook were bypassed, the original NAT'd
    # LAN address would remain and subsequent requests would fail to route.
    device.rewrite_url.assert_called_once_with(NORMALIZED_ADDRESS)
    assert device.xaddrs[pullpoint_key] == "http://wan.example.com:9000/rewritten"


@pytest.mark.asyncio
async def test_notification_manager_start_rewrites_subscription_address_on_nat_override() -> (
    None
):
    """NotificationManager also routes consumer addresses through rewrite_url."""
    device = _make_device()
    device.rewrite_url = Mock(return_value="http://wan.example.com:9000/consumer")
    mgr, _notify_service, _subscription, _operation = _make_notification_manager(device)

    await mgr._start()

    consumer_key = "http://www.onvif.org/ver10/events/wsdl/NotificationConsumer"
    device.rewrite_url.assert_called_once_with(NORMALIZED_ADDRESS)
    assert device.xaddrs[consumer_key] == "http://wan.example.com:9000/consumer"


@pytest.mark.asyncio
async def test_pullpoint_manager_get_service() -> None:
    device = _make_device()
    mgr, _subscription, pullpoint_service = _make_pullpoint_manager(device)

    await mgr._start()

    assert mgr.get_service() is pullpoint_service
