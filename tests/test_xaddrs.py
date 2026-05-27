"""Tests for service XAddr discovery in update_xaddrs."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
import pytest_asyncio
from zeep.exceptions import Fault

import onvif
from onvif import ONVIFCamera
from onvif.exceptions import ONVIFError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

WSDL_DIR = str(Path(onvif.__file__).parent / "wsdl")

RECORDING_NS = "http://www.onvif.org/ver10/recording/wsdl"
REPLAY_NS = "http://www.onvif.org/ver10/replay/wsdl"
SEARCH_NS = "http://www.onvif.org/ver10/search/wsdl"
MEDIA_NS = "http://www.onvif.org/ver10/media/wsdl"
EVENTS_NS = "http://www.onvif.org/ver10/events/wsdl"


def _top_level_capabilities() -> dict:
    """GetCapabilities response advertising only top-level services.

    Recording/Replay/Search are never present at the top level -- ONVIF
    nests them under the Extension element, so this mirrors what a real
    device returns from GetCapabilities. The Extension/Recording entry is
    included precisely to show it is *not* a top-level key the fallback can
    surface.
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
    cam = ONVIFCamera("192.168.1.100", 80, "admin", "password", wsdl_dir=WSDL_DIR)
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
    devicemgmt.GetCapabilities = AsyncMock(return_value=_top_level_capabilities())
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
    # GetServices is tried first; when it succeeds GetCapabilities is skipped to
    # avoid a redundant round-trip.
    devicemgmt.GetCapabilities.assert_not_called()


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


@pytest.mark.parametrize(
    "error",
    [
        # Awaited zeep service calls raise zeep.exceptions.Fault directly --
        # safe_func only wraps the synchronous request build, not the await --
        # so this is what _update_xaddrs_from_services actually sees on a device
        # that does not implement GetServices.
        Fault("not supported"),
        # Other failures (e.g. ONVIFTimeoutError) surface as ONVIFError.
        ONVIFError("boom"),
    ],
)
@pytest.mark.asyncio
async def test_update_xaddrs_falls_back_when_get_services_unsupported(
    camera: ONVIFCamera,
    error: Exception,
) -> None:
    """Devices without GetServices fall back to GetCapabilities and don't crash."""
    failing_get_services = AsyncMock(side_effect=error)
    devicemgmt = _mock_devicemgmt(get_services=failing_get_services)
    with patch.object(
        camera, "create_devicemgmt_service", AsyncMock(return_value=devicemgmt)
    ):
        await camera.update_xaddrs()

    # GetServices failed, so we fell back to GetCapabilities.
    devicemgmt.GetCapabilities.assert_called_once()
    # Top-level capabilities are still discovered.
    assert camera.xaddrs[MEDIA_NS] == "http://192.168.1.100/onvif/media_service"
    # Recording remains undiscoverable (it is nested under Extension, not a
    # top-level GetCapabilities key), surfacing the standard error.
    with pytest.raises(ONVIFError):
        camera.get_definition("recording")


@pytest.mark.asyncio
async def test_update_xaddrs_falls_back_when_get_services_empty(
    camera: ONVIFCamera,
) -> None:
    """An empty GetServices response also triggers the GetCapabilities fallback."""
    devicemgmt = _mock_devicemgmt(get_services=AsyncMock(return_value=[]))
    with patch.object(
        camera, "create_devicemgmt_service", AsyncMock(return_value=devicemgmt)
    ):
        await camera.update_xaddrs()

    devicemgmt.GetCapabilities.assert_called_once()
    assert camera.xaddrs[MEDIA_NS] == "http://192.168.1.100/onvif/media_service"


@pytest.mark.asyncio
async def test_get_capabilities_lazily_fetches_via_get_capabilities(
    camera: ONVIFCamera,
) -> None:
    """get_capabilities() populates _capabilities even when XAddrs came from GetServices."""
    devicemgmt = _mock_devicemgmt()
    with patch.object(
        camera, "create_devicemgmt_service", AsyncMock(return_value=devicemgmt)
    ):
        await camera.update_xaddrs()
        # GetServices supplied the XAddrs, so GetCapabilities was not called yet.
        devicemgmt.GetCapabilities.assert_not_called()

        capabilities = await camera.get_capabilities()

    devicemgmt.GetCapabilities.assert_called_once()
    assert capabilities["Media"]["XAddr"] == "http://192.168.1.100/onvif/media_service"


@pytest.mark.asyncio
async def test_update_xaddrs_skips_malformed_capability_entries(
    camera: ONVIFCamera,
) -> None:
    """A GetCapabilities entry missing its XAddr is skipped without crashing."""
    bad_caps = {
        "Media": {"foo": "bar"},  # in SERVICES but no XAddr -> skipped
        "Events": {"XAddr": "http://192.168.1.100/onvif/events_service"},
    }
    devicemgmt = _mock_devicemgmt(get_services=AsyncMock(side_effect=Fault("x")))
    devicemgmt.GetCapabilities = AsyncMock(return_value=bad_caps)
    with patch.object(
        camera, "create_devicemgmt_service", AsyncMock(return_value=devicemgmt)
    ):
        await camera.update_xaddrs()

    # The malformed Media entry is dropped...
    assert MEDIA_NS not in camera.xaddrs
    # ...while the well-formed Events entry is still discovered.
    assert camera.xaddrs[EVENTS_NS] == "http://192.168.1.100/onvif/events_service"


@pytest.mark.asyncio
async def test_update_xaddrs_logs_malformed_capabilities_at_debug(
    camera: ONVIFCamera,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed capability entries log at DEBUG -- not as an exception traceback."""
    bad_caps = {
        "Media": {"foo": "bar"},  # missing XAddr -> KeyError -> debug log
        "Events": {"XAddr": "http://192.168.1.100/onvif/events_service"},
    }
    devicemgmt = _mock_devicemgmt(get_services=AsyncMock(side_effect=Fault("x")))
    devicemgmt.GetCapabilities = AsyncMock(return_value=bad_caps)
    with (
        patch.object(
            camera, "create_devicemgmt_service", AsyncMock(return_value=devicemgmt)
        ),
        caplog.at_level(logging.DEBUG, logger="onvif.client"),
    ):
        await camera.update_xaddrs()

    # No ERROR/EXCEPTION-level records from the onvif logger: malformed
    # entries are an expected condition, not a bug-surfacing crash. Filter by
    # logger so unrelated noise (e.g. asyncio's GC warning for a stray
    # ClientSession leaked by another test) cannot flake this assertion.
    onvif_records = [r for r in caplog.records if r.name == "onvif"]
    high_severity = [r for r in onvif_records if r.levelno >= logging.WARNING]
    assert high_severity == []
    # The skip is still observable in debug output so operators can diagnose.
    assert any(
        r.levelno == logging.DEBUG and "Media" in r.getMessage() for r in onvif_records
    )


@pytest.mark.asyncio
async def test_update_xaddrs_propagates_unexpected_capability_errors(
    camera: ONVIFCamera,
) -> None:
    """Unexpected exception types from capability lookup are not swallowed.

    Narrow handling catches only the parse-error shapes (KeyError/TypeError/
    AttributeError); anything else is a genuine bug and must surface so it
    can be diagnosed rather than hide behind a log line.
    """

    class _ExplodingCapability(dict):
        def __getitem__(self, key):
            if key == "XAddr":
                msg = "boom"
                raise RuntimeError(msg)
            return super().__getitem__(key)

    bad_caps = {"Media": _ExplodingCapability(XAddr="ignored")}
    devicemgmt = _mock_devicemgmt(get_services=AsyncMock(side_effect=Fault("x")))
    devicemgmt.GetCapabilities = AsyncMock(return_value=bad_caps)
    with (
        patch.object(
            camera, "create_devicemgmt_service", AsyncMock(return_value=devicemgmt)
        ),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await camera.update_xaddrs()


@pytest.mark.asyncio
async def test_update_xaddrs_survives_unserializable_capabilities(
    camera: ONVIFCamera,
) -> None:
    """A capabilities payload that cannot be serialized doesn't crash update_xaddrs."""

    class _Unserializable(dict):
        def __iter__(self):
            msg = "cannot serialize"
            raise ValueError(msg)

    bad_caps = {"Extension": _Unserializable()}
    devicemgmt = _mock_devicemgmt(get_services=AsyncMock(side_effect=Fault("x")))
    devicemgmt.GetCapabilities = AsyncMock(return_value=bad_caps)
    with patch.object(
        camera, "create_devicemgmt_service", AsyncMock(return_value=devicemgmt)
    ):
        # Parsing failure is best-effort and swallowed; update_xaddrs completes.
        await camera.update_xaddrs()

    assert camera._capabilities is None


@pytest.mark.asyncio
async def test_update_xaddrs_skips_malformed_service_entries(
    camera: ONVIFCamera,
) -> None:
    """Entries missing Namespace/XAddr are skipped without aborting discovery."""
    malformed = [
        Mock(spec=[]),  # no Namespace/XAddr attributes -> AttributeError
        Mock(Namespace=REPLAY_NS, XAddr=None),  # falsy XAddr -> skipped
        Mock(Namespace=None, XAddr="http://192.168.1.100/onvif/x"),  # falsy ns
        Mock(
            Namespace=RECORDING_NS,
            XAddr="http://192.168.1.100/onvif/recording_service",
        ),
    ]
    devicemgmt = _mock_devicemgmt(get_services=AsyncMock(return_value=malformed))
    with patch.object(
        camera, "create_devicemgmt_service", AsyncMock(return_value=devicemgmt)
    ):
        await camera.update_xaddrs()

    # The single well-formed entry is still discovered...
    assert camera.xaddrs[RECORDING_NS] == "http://192.168.1.100/onvif/recording_service"
    # ...while the malformed ones are dropped.
    assert REPLAY_NS not in camera.xaddrs
    # One good entry means GetServices "succeeded" -- no fallback round-trip.
    devicemgmt.GetCapabilities.assert_not_called()


@pytest.mark.asyncio
async def test_update_xaddrs_adjust_time_retries_with_auth_on_fault() -> None:
    """adjust_time retries the clock probe with auth when the authless call faults.

    Cameras that reject the unauthenticated GetSystemDateAndTime probe answer
    with a zeep Fault; the adjust_time handshake must then retry the call with
    credentials rather than abort. Covers the authless->Fault->authenticated
    fallback in the clock-skew handshake.
    """
    cam = ONVIFCamera(
        "192.168.1.100",
        80,
        "admin",
        "password",
        wsdl_dir=WSDL_DIR,
        adjust_time=True,
    )
    sys_date = Mock()
    sys_date.UTCDateTime = Mock(
        Date=Mock(Year=2024, Month=8, Day=17),
        Time=Mock(Hour=12, Minute=30, Second=45),
    )
    devicemgmt = _mock_devicemgmt()
    devicemgmt.binding_key = "devicemgmt"
    # The authless probe faults (camera demands auth); the authenticated retry
    # then succeeds and supplies the device clock.
    devicemgmt.authless_GetSystemDateAndTime = AsyncMock(side_effect=Fault("auth"))
    devicemgmt.GetSystemDateAndTime = AsyncMock(return_value=sys_date)
    cam.services[devicemgmt.binding_key] = devicemgmt

    try:
        with patch.object(
            cam, "create_devicemgmt_service", AsyncMock(return_value=devicemgmt)
        ):
            await cam.update_xaddrs()
    finally:
        await cam.close()

    # The authless probe faulted, so the handshake retried with credentials...
    devicemgmt.authless_GetSystemDateAndTime.assert_awaited_once()
    devicemgmt.GetSystemDateAndTime.assert_awaited_once()
    # ...and the device clock offset was still computed.
    assert cam.dt_diff is not None
    # Discovery then proceeded normally via GetServices.
    assert cam.xaddrs[RECORDING_NS] == "http://192.168.1.100/onvif/recording_service"
