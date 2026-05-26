"""End-to-end tests driving ONVIFCamera against a fake Hikvision camera.

These tests exercise the full client pipeline -- WSDL loading, WS-Security
signing, the aiohttp transport and zeep response parsing -- against a real
in-process HTTP server (:mod:`tests.fake_hikvision`) that answers like a
Hikvision IP camera. They are deliberately "end to end": nothing inside
:mod:`onvif` is mocked, only the camera on the other end of the socket is fake.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

# fake_hikvision is a sibling test helper; pytest puts tests/ on sys.path.
from fake_hikvision import WSSE_NS, FakeHikvisionCamera

import onvif
from onvif import ONVIFCamera

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

WSDL_DIR = str(Path(onvif.__file__).parent / "wsdl")
DIGEST_TYPE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
)
TEXT_TYPE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-username-token-profile-1.0#PasswordText"
)


@pytest_asyncio.fixture
async def fake_camera() -> AsyncGenerator[FakeHikvisionCamera]:
    """Start a fake Hikvision camera and tear it down afterwards."""
    camera = FakeHikvisionCamera()
    await camera.start()
    try:
        yield camera
    finally:
        await camera.stop()


@pytest_asyncio.fixture
async def onvif_camera(
    fake_camera: FakeHikvisionCamera,
) -> AsyncGenerator[ONVIFCamera]:
    """Create an ONVIFCamera pointed at the fake Hikvision camera."""
    cam = ONVIFCamera(
        fake_camera.host,
        fake_camera.port,
        "admin",
        "Password1",
        wsdl_dir=WSDL_DIR,
        no_cache=True,
    )
    try:
        yield cam
    finally:
        await cam.close()


@pytest.mark.asyncio
async def test_get_device_information(
    fake_camera: FakeHikvisionCamera, onvif_camera: ONVIFCamera
) -> None:
    """GetDeviceInformation round-trips and reports the Hikvision identity."""
    devicemgmt = await onvif_camera.create_devicemgmt_service()
    info = await devicemgmt.GetDeviceInformation()

    assert info.Manufacturer == "HIKVISION"
    assert info.Model == "DS-2CD2085FWD-I"
    assert info.FirmwareVersion == "V5.5.82 build 190909"
    assert info.HardwareId == "88"

    # The request hit the fixed device service path.
    request = fake_camera.last_request("GetDeviceInformation")
    assert request is not None
    assert request.path == "/onvif/device_service"


@pytest.mark.asyncio
async def test_update_xaddrs_discovers_services_via_get_services(
    fake_camera: FakeHikvisionCamera, onvif_camera: ONVIFCamera
) -> None:
    """update_xaddrs prefers GetServices and records every advertised XAddr.

    GetServices surfaces services nested under the GetCapabilities Extension
    element (Recording/Replay/Search) that the GetCapabilities parser drops, so
    they appear here only because GetServices is queried first.
    """
    await onvif_camera.update_xaddrs()

    base = fake_camera.base_url
    assert onvif_camera.xaddrs == {
        "http://www.onvif.org/ver10/device/wsdl": f"{base}/onvif/device_service",
        "http://www.onvif.org/ver10/media/wsdl": f"{base}/onvif/Media",
        "http://www.onvif.org/ver10/events/wsdl": f"{base}/onvif/Events",
        "http://www.onvif.org/ver20/imaging/wsdl": f"{base}/onvif/Imaging",
        "http://www.onvif.org/ver20/ptz/wsdl": f"{base}/onvif/PTZ",
        "http://www.onvif.org/ver20/analytics/wsdl": f"{base}/onvif/Analytics",
        "http://www.onvif.org/ver10/recording/wsdl": f"{base}/onvif/Recording",
        "http://www.onvif.org/ver10/replay/wsdl": f"{base}/onvif/Replay",
        "http://www.onvif.org/ver10/search/wsdl": f"{base}/onvif/Search",
    }
    # GetServices satisfied discovery, so the GetCapabilities round-trip is
    # skipped entirely.
    assert fake_camera.last_request("GetServices") is not None
    assert fake_camera.last_request("GetCapabilities") is None


@pytest.mark.asyncio
async def test_update_xaddrs_falls_back_to_capabilities_when_get_services_broken() -> (
    None
):
    """A device with broken GetServices still discovers its top-level services.

    Older devices answer GetServices with a fault; update_xaddrs must fall back
    to GetCapabilities rather than crashing. Recording/Replay/Search live under
    the Extension element and so stay undiscoverable on the fallback path.
    """
    camera = FakeHikvisionCamera(broken_get_services=True)
    await camera.start()
    cam = ONVIFCamera(
        camera.host,
        camera.port,
        "admin",
        "Password1",
        wsdl_dir=WSDL_DIR,
        no_cache=True,
    )
    try:
        await cam.update_xaddrs()
        base = camera.base_url
        # Only the top-level GetCapabilities services are known after fallback.
        assert cam.xaddrs == {
            "http://www.onvif.org/ver20/analytics/wsdl": f"{base}/onvif/Analytics",
            "http://www.onvif.org/ver10/events/wsdl": f"{base}/onvif/Events",
            "http://www.onvif.org/ver20/imaging/wsdl": f"{base}/onvif/Imaging",
            "http://www.onvif.org/ver10/media/wsdl": f"{base}/onvif/Media",
            "http://www.onvif.org/ver20/ptz/wsdl": f"{base}/onvif/PTZ",
        }
        # Recording is nested under Extension -- not surfaced by the fallback.
        assert "http://www.onvif.org/ver10/recording/wsdl" not in cam.xaddrs
        # GetServices was attempted (and failed) before falling back.
        assert camera.last_request("GetServices") is not None
        assert camera.last_request("GetCapabilities") is not None
    finally:
        await cam.close()
        await camera.stop()


@pytest.mark.asyncio
async def test_get_capabilities_returns_dict(onvif_camera: ONVIFCamera) -> None:
    """get_capabilities exposes the parsed capabilities as a dictionary."""
    capabilities = await onvif_camera.get_capabilities()

    assert capabilities["Media"]["XAddr"].endswith("/onvif/Media")
    assert capabilities["Events"]["WSPullPointSupport"] is True
    assert capabilities["Analytics"]["RuleSupport"] is True


@pytest.mark.asyncio
async def test_get_capabilities_adjusts_time_when_called_before_update_xaddrs() -> None:
    """get_capabilities runs the adjust_time handshake when it is the first call.

    GetServices does not return the capabilities structure, so _capabilities is
    populated lazily by get_capabilities(). That path must still perform the
    adjust_time clock-skew compensation update_xaddrs() does; otherwise a
    caller invoking get_capabilities() first on a clock-skewed camera would sign
    GetCapabilities with an un-adjusted timestamp.
    """
    camera = FakeHikvisionCamera()
    # Skew the device clock well away from the host so dt_diff is non-zero.
    camera.utc_year = 2020
    await camera.start()
    cam = ONVIFCamera(
        camera.host,
        camera.port,
        "admin",
        "Password1",
        wsdl_dir=WSDL_DIR,
        no_cache=True,
        adjust_time=True,
    )
    try:
        assert cam.dt_diff is None
        capabilities = await cam.get_capabilities()

        # The adjust_time handshake ran: dt_diff was computed from the device
        # clock and GetSystemDateAndTime was queried.
        assert cam.dt_diff is not None
        assert camera.last_request("GetSystemDateAndTime") is not None
        assert capabilities["Media"]["XAddr"].endswith("/onvif/Media")
    finally:
        await cam.close()
        await camera.stop()


@pytest.mark.asyncio
async def test_get_capabilities_reuses_dt_diff_from_update_xaddrs() -> None:
    """get_capabilities reuses the offset update_xaddrs computed.

    The shared _devicemgmt_with_time() prologue runs the adjust_time handshake
    only when dt_diff is unset. Once update_xaddrs() has populated dt_diff, a
    later get_capabilities() must not repeat the GetSystemDateAndTime
    round-trip.
    """
    camera = FakeHikvisionCamera()
    # Skew the device clock so the handshake computes a non-zero dt_diff.
    camera.utc_year = 2020
    await camera.start()
    cam = ONVIFCamera(
        camera.host,
        camera.port,
        "admin",
        "Password1",
        wsdl_dir=WSDL_DIR,
        no_cache=True,
        adjust_time=True,
    )
    try:
        await cam.update_xaddrs()
        assert cam.dt_diff is not None
        # update_xaddrs ran the handshake exactly once.
        assert camera.operations().count("GetSystemDateAndTime") == 1

        capabilities = await cam.get_capabilities()

        # dt_diff was already set, so the handshake is not repeated.
        assert camera.operations().count("GetSystemDateAndTime") == 1
        assert capabilities["Media"]["XAddr"].endswith("/onvif/Media")
    finally:
        await cam.close()
        await camera.stop()


@pytest.mark.asyncio
async def test_get_system_date_and_time(
    fake_camera: FakeHikvisionCamera, onvif_camera: ONVIFCamera
) -> None:
    """GetSystemDateAndTime parses the structured UTC date/time elements."""
    fake_camera.utc_year = 2024
    fake_camera.utc_month = 8
    fake_camera.utc_day = 17
    fake_camera.utc_hour = 23
    fake_camera.utc_minute = 59
    fake_camera.utc_second = 58

    devicemgmt = await onvif_camera.create_devicemgmt_service()
    # zeep unwraps the single-element response, so this is the SystemDateAndTime
    # object directly (the same shape update_xaddrs consumes).
    result = await devicemgmt.GetSystemDateAndTime()

    utc = result.UTCDateTime
    assert (utc.Date.Year, utc.Date.Month, utc.Date.Day) == (2024, 8, 17)
    assert (utc.Time.Hour, utc.Time.Minute, utc.Time.Second) == (23, 59, 58)
    assert result.DateTimeType == "NTP"


@pytest.mark.asyncio
async def test_media_snapshot_uri(
    fake_camera: FakeHikvisionCamera, onvif_camera: ONVIFCamera
) -> None:
    """The media service returns the snapshot URI advertised by the camera."""
    # update_xaddrs first so the media XAddr is known.
    await onvif_camera.update_xaddrs()
    uri = await onvif_camera.get_snapshot_uri("Profile_1")

    assert uri == f"{fake_camera.base_url}/onvif-http/snapshot?Profile_1"
    request = fake_camera.last_request("GetSnapshotUri")
    assert request is not None
    assert request.path == "/onvif/Media"


@pytest.mark.asyncio
async def test_requests_use_digest_auth_with_zulu_timestamp(
    fake_camera: FakeHikvisionCamera, onvif_camera: ONVIFCamera
) -> None:
    """The client signs requests with a PasswordDigest and Zulu Created stamp.

    Hikvision firmware rejects the numeric "+00:00" offset that zeep emits by
    default, so the library forces canonical UTC "Zulu" timestamps. This is the
    regression guarded end-to-end here: the bytes the fake camera receives must
    carry a digest password and a ``Created`` element ending in ``Z``.

    See https://github.com/openvideolibs/python-onvif-zeep-async/issues/179
    """
    devicemgmt = await onvif_camera.create_devicemgmt_service()
    await devicemgmt.GetDeviceInformation()

    request = fake_camera.last_request("GetDeviceInformation")
    assert request is not None

    assert request.username == "admin"
    assert request.password_type == DIGEST_TYPE

    created = request.created
    assert created is not None
    assert created.endswith("Z")
    assert "+00:00" not in created

    # A nonce must accompany the digest.
    nonce = request.envelope.find(f".//{{{WSSE_NS}}}Nonce")
    assert nonce is not None
    assert nonce.text


@pytest.mark.asyncio
async def test_plaintext_auth_when_encryption_disabled(
    fake_camera: FakeHikvisionCamera,
) -> None:
    """With encrypt=False the password is sent as PasswordText, not a digest."""
    cam = ONVIFCamera(
        fake_camera.host,
        fake_camera.port,
        "admin",
        "Password1",
        wsdl_dir=WSDL_DIR,
        no_cache=True,
        encrypt=False,
    )
    try:
        devicemgmt = await cam.create_devicemgmt_service()
        await devicemgmt.GetDeviceInformation()
    finally:
        await cam.close()

    request = fake_camera.last_request("GetDeviceInformation")
    assert request is not None
    assert request.password_type == TEXT_TYPE


@pytest.mark.asyncio
async def test_pullpoint_subscription_parses_hikvision_dash_timestamps(
    fake_camera: FakeHikvisionCamera, onvif_camera: ONVIFCamera
) -> None:
    """CreatePullPointSubscription parses the Hikvision "-" separator timestamps.

    Hikvision firmware sends xs:dateTime values such as ``2024-08-17-12:30:45Z``
    (a "-" instead of "T" between date and time). Parsing only succeeds because
    of the FastDateTime workaround, so exercise it through the real events WSDL.

    See https://github.com/openvideolibs/python-onvif-zeep-async/issues/178
    """
    fake_camera.pullpoint_current_time = "2024-08-17-12:30:45Z"
    fake_camera.pullpoint_termination_time = "2024-08-17-12:31:45Z"

    await onvif_camera.update_xaddrs()
    events = await onvif_camera.create_events_service()
    result = await events.CreatePullPointSubscription(
        {"InitialTerminationTime": "PT60S"}
    )

    # The dash-separated timestamps round-tripped into real datetimes.
    assert result.CurrentTime == dt.datetime(
        2024, 8, 17, 12, 30, 45, tzinfo=dt.timezone.utc
    )
    assert result.TerminationTime == dt.datetime(
        2024, 8, 17, 12, 31, 45, tzinfo=dt.timezone.utc
    )
    # The subscription endpoint reference parsed too.
    assert result.SubscriptionReference.Address._value_1.endswith(
        "/onvif/Subscription?Idx=1"
    )


@pytest.mark.asyncio
async def test_broken_relative_time_detection_from_pullpoint(
    fake_camera: FakeHikvisionCamera, onvif_camera: ONVIFCamera
) -> None:
    """A short subscription window is flagged as broken relative timestamps.

    When the gap between CurrentTime and TerminationTime is far smaller than the
    requested interval, the client treats the device as having broken relative
    timestamps and switches to absolute termination times.
    """
    # 60-second window but we will ask about a 5-minute interval.
    fake_camera.pullpoint_current_time = "2024-08-17-12:30:45Z"
    fake_camera.pullpoint_termination_time = "2024-08-17-12:31:45Z"

    await onvif_camera.update_xaddrs()
    events = await onvif_camera.create_events_service()
    result = await events.CreatePullPointSubscription(
        {"InitialTerminationTime": "PT300S"}
    )

    assert (
        onvif_camera.has_broken_relative_time(
            dt.timedelta(seconds=300),
            result.CurrentTime,
            result.TerminationTime,
        )
        is True
    )
    # With absolute timestamps enabled the next termination time is an ISO
    # 8601 instant ending in Z rather than a relative PT…S duration.
    next_termination = onvif_camera.get_next_termination_time(dt.timedelta(seconds=300))
    assert next_termination.endswith("Z")
    assert not next_termination.startswith("PT")


@pytest.mark.asyncio
async def test_camera_rejecting_unauthenticated_requests() -> None:
    """A camera requiring auth still completes once the client signs requests.

    require_auth makes the fake camera return a SOAP fault for any request that
    lacks a WS-Security UsernameToken. Because ONVIFCamera always signs its
    requests, the call should succeed and the camera should have seen the token.
    """
    camera = FakeHikvisionCamera(require_auth=True)
    await camera.start()
    cam = ONVIFCamera(
        camera.host,
        camera.port,
        "admin",
        "Password1",
        wsdl_dir=WSDL_DIR,
        no_cache=True,
    )
    try:
        devicemgmt = await cam.create_devicemgmt_service()
        info = await devicemgmt.GetDeviceInformation()
        assert info.Manufacturer == "HIKVISION"
    finally:
        await cam.close()
        await camera.stop()

    request = camera.last_request("GetDeviceInformation")
    assert request is not None
    assert request.username == "admin"
