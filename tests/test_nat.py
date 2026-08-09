"""Tests for NAT URL rewriting."""

from __future__ import annotations

import datetime as dt
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from onvif import ONVIFCamera
from onvif.managers import NotificationManager, PullPointManager
from onvif.util import replace_host_port

_WSDL_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "onvif", "wsdl")
MEDIA_NS = "http://www.onvif.org/ver10/media/wsdl"


def test_replace_host_port_strips_scheme_from_constructor_host() -> None:
    """A scheme-prefixed constructor host must not create a double scheme."""
    assert (
        replace_host_port(
            "http://192.168.1.10/onvif/media",
            "https://203.0.113.5",
            8443,
        )
        == "http://203.0.113.5:8443/onvif/media"
    )


def test_replace_host_port_handles_ipv6_hosts() -> None:
    """Bare and bracketed IPv6 constructor hosts produce valid netlocs."""
    assert (
        replace_host_port("http://192.168.1.10/service", "2001:db8::5", 8080)
        == "http://[2001:db8::5]:8080/service"
    )
    assert (
        replace_host_port("http://192.168.1.10/service", "[2001:db8::5]", 8080)
        == "http://[2001:db8::5]:8080/service"
    )


@pytest.mark.asyncio
async def test_rewrite_url_handles_port_only_nat() -> None:
    """A missing advertised port must not bypass NAT rewriting."""
    camera = ONVIFCamera(
        "203.0.113.5", 8080, "user", "pass", wsdl_dir=_WSDL_PATH, nat_override=True
    )
    try:
        assert (
            camera.rewrite_url("http://203.0.113.5/service")
            == "http://203.0.113.5:8080/service"
        )
    finally:
        await camera.close()


@pytest.mark.asyncio
async def test_update_xaddrs_rewrites_services_when_nat_override() -> None:
    """NAT override rewrites service XAddrs to the caller's host and port."""
    camera = ONVIFCamera(
        "203.0.113.5",
        8080,
        "user",
        "pass",
        wsdl_dir=_WSDL_PATH,
        nat_override=True,
    )
    devicemgmt = Mock()
    devicemgmt.GetServices = AsyncMock(
        return_value=[
            Mock(
                Namespace=MEDIA_NS,
                XAddr="http://192.168.1.10/onvif/media_service",
            )
        ]
    )
    try:
        with patch.object(
            camera,
            "create_devicemgmt_service",
            AsyncMock(return_value=devicemgmt),
        ):
            await camera.update_xaddrs()
    finally:
        await camera.close()

    assert camera.xaddrs[MEDIA_NS] == "http://203.0.113.5:8080/onvif/media_service"


@pytest.mark.asyncio
async def test_snapshot_uri_is_rewritten_when_nat_override() -> None:
    """Snapshot URLs must follow the same NAT rewrite as service XAddrs."""
    camera = ONVIFCamera(
        "https://203.0.113.5",
        8443,
        "user",
        "pass",
        wsdl_dir=_WSDL_PATH,
        nat_override=True,
    )
    service = Mock()
    service.create_type = Mock(return_value=Mock())
    service.GetSnapshotUri = AsyncMock(
        return_value=Mock(Uri="http://192.168.1.10/snapshot")
    )
    try:
        with patch.object(
            camera,
            "create_media_service",
            AsyncMock(return_value=service),
        ):
            assert (
                await camera.get_snapshot_uri("Profile1")
                == "http://203.0.113.5:8443/snapshot"
            )
    finally:
        await camera.close()


@pytest.mark.asyncio
async def test_subscription_urls_are_rewritten_with_nat_override() -> None:
    """Subscription managers use the camera's actual NAT rewrite."""
    camera = ONVIFCamera(
        "203.0.113.5", 8080, "user", "pass", wsdl_dir=_WSDL_PATH, nat_override=True
    )
    result = SimpleNamespace(
        SubscriptionReference=SimpleNamespace(
            Address=SimpleNamespace(
                _value_1="http://192.168.1.10:80/onvif/subscription"
            )
        ),
        CurrentTime=None,
        TerminationTime=None,
    )
    subscription = Mock()
    subscription.Renew = AsyncMock()
    events = Mock(CreatePullPointSubscription=AsyncMock(return_value=result))
    camera.create_events_service = AsyncMock(return_value=events)
    camera.create_subscription_service = AsyncMock(return_value=subscription)
    camera.create_pullpoint_service = AsyncMock(return_value=Mock())
    camera.has_broken_relative_time = Mock(return_value=False)

    try:
        manager = PullPointManager(camera, dt.timedelta(seconds=60), Mock())
        await manager._start()

        key = "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription"
        assert camera.xaddrs[key] == "http://203.0.113.5:8080/onvif/subscription"

        notification = Mock(Subscribe=AsyncMock(return_value=result))
        consumer = Mock(
            document=Mock(bindings={"binding": Mock()}), binding_name="binding"
        )
        consumer.document.bindings["binding"].get = Mock(return_value=Mock())
        camera.create_notification_service = AsyncMock(return_value=notification)
        camera.create_onvif_service = AsyncMock(return_value=consumer)

        manager = NotificationManager(
            camera, "http://callback", dt.timedelta(seconds=60), Mock()
        )
        await manager._start()

        key = "http://www.onvif.org/ver10/events/wsdl/NotificationConsumer"
        assert camera.xaddrs[key] == "http://203.0.113.5:8080/onvif/subscription"
    finally:
        await camera.close()
