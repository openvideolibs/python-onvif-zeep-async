"""Tests for service XAddr discovery in update_xaddrs."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock, patch

import pytest_asyncio

import onvif
import pytest
from onvif import ONVIFCamera
from onvif.exceptions import ONVIFError

WSDL_DIR = os.path.join(os.path.dirname(onvif.__file__), "wsdl")

RECORDING_NS = "http://www.onvif.org/ver10/recording/wsdl"
REPLAY_NS = "http://www.onvif.org/ver10/replay/wsdl"
SEARCH_NS = "http://www.onvif.org/ver10/search/wsdl"
MEDIA_NS = "http://www.onvif.org/ver10/media/wsdl"


def _capabilities_without_recording() -> dict:
    """GetCapabilities response advertising only top-level services.

    Recording/Replay/Search are never present at the top level -- ONVIF
    nests them under the Extension element, so this mirrors what a real
    device returns from GetCapabilities.
    """
    return {
        "Media": {"XAddr": "http://192.168.1.100/onvif/media_service"},
        "Events": {"XAddr": "http://192.168.1.100/onvif/events_service"},
        "Extension": {
            "Recording": {"XAddr": "http://192.168.1.100/onvif/recording_service"},
        },
    }


def _services_response() -> list[Mock]:
    """GetServices response advertising the full set of services."""
    return [
        Mock(Namespace=MEDIA_NS, XAddr="http://192.168.1.100/onvif/media_service"),
        Mock(
            Namespace=RECORDING_NS,
            XAddr="http://192.168.1.100/onvif/recording_service",
        ),
        Mock(Namespace=REPLAY_NS, XAddr="http://192.168.1.100/onvif/replay_service"),
        Mock(Namespace=SEARCH_NS, XAddr="http://192.168.1.100/onvif/search_service"),
    ]


@asynccontextmanager
async def _create_camera() -> AsyncGenerator[ONVIFCamera]:
    cam = ONVIFCamera("192.168.1.100", 80, "admin", "password", wsdl_dir=WSDL_DIR)  # noqa: S106
    try:
        yield cam
    finally:
        await cam.close()


@pytest_asyncio.fixture
async def camera() -> AsyncGenerator[ONVIFCamera]:
    async with _create_camera() as cam:
        yield cam


def _mock_devicemgmt(get_services: AsyncMock | None = None) -> Mock:
    devicemgmt = Mock()
    devicemgmt.GetCapabilities = AsyncMock(
        return_value=_capabilities_without_recording()
    )
    devicemgmt.GetServices = get_services or AsyncMock(
        return_value=_services_response()
    )
    devicemgmt.close = AsyncMock()
    return devicemgmt


@pytest.mark.asyncio
async def test_update_xaddrs_discovers_recording_via_get_services(
    camera: ONVIFCamera,
) -> None:
    """Recording/Replay/Search XAddrs come from GetServices, not GetCapabilities."""
    devicemgmt = _mock_devicemgmt()
    with patch.object(
        camera, "create_devicemgmt_service", AsyncMock(return_value=devicemgmt)
    ):
        await camera.update_xaddrs()

    assert camera.xaddrs[RECORDING_NS] == "http://192.168.1.100/onvif/recording_service"
    assert camera.xaddrs[REPLAY_NS] == "http://192.168.1.100/onvif/replay_service"
    assert camera.xaddrs[SEARCH_NS] == "http://192.168.1.100/onvif/search_service"


@pytest.mark.asyncio
async def test_get_definition_resolves_recording_after_update(
    camera: ONVIFCamera,
) -> None:
    """get_definition('recording') no longer raises once XAddrs are populated."""
    devicemgmt = _mock_devicemgmt()
    with patch.object(
        camera, "create_devicemgmt_service", AsyncMock(return_value=devicemgmt)
    ):
        await camera.update_xaddrs()

    xaddr, _wsdl, binding_name = camera.get_definition("recording")
    assert xaddr == "http://192.168.1.100/onvif/recording_service"
    assert binding_name == f"{{{RECORDING_NS}}}RecordingBinding"


@pytest.mark.asyncio
async def test_update_xaddrs_falls_back_when_get_services_unsupported(
    camera: ONVIFCamera,
) -> None:
    """Older devices without GetServices still get top-level XAddrs and don't crash."""
    failing_get_services = AsyncMock(side_effect=Exception("not supported"))
    devicemgmt = _mock_devicemgmt(get_services=failing_get_services)
    with patch.object(
        camera, "create_devicemgmt_service", AsyncMock(return_value=devicemgmt)
    ):
        await camera.update_xaddrs()

    # Top-level capabilities are still discovered.
    assert camera.xaddrs[MEDIA_NS] == "http://192.168.1.100/onvif/media_service"
    # Recording remains undiscoverable, surfacing the standard error.
    with pytest.raises(ONVIFError):
        camera.get_definition("recording")
