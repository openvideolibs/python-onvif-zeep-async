"""Unit tests for :mod:`onvif.client`.

These cover the parts of :class:`~onvif.client.ONVIFCamera`,
:class:`~onvif.client.ONVIFService` and :class:`~onvif.client.ZeepAsyncClient`
that are pure logic or thin wrappers -- service-definition resolution, the
broken-relative-timestamp heuristic, termination-time formatting, the manager
factories and the per-service creation helpers -- without any network or WSDL
loading. The zeep/aiohttp dependencies they delegate to are mocked.
"""

from __future__ import annotations

import datetime as dt
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest

import onvif.client
from onvif import ONVIFCamera
from onvif.client import (
    _WSDL_DIR_FILES,
    _WSDL_PATH,
    ONVIFService,
    ZeepAsyncClient,
    _get_shared_sqlite_cache,
    _list_wsdl_dir,
)
from onvif.exceptions import ONVIFAuthError, ONVIFError, ONVIFTimeoutError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

# The bundled WSDL files live under onvif/wsdl, not the default _WSDL_PATH
# (which points one level above the package). Resolve the real directory so the
# get_definition() tests can exercise the on-disk path checks.
_REAL_WSDL_DIR = str(Path(onvif.client.__file__).parent / "wsdl")


@asynccontextmanager
async def create_test_camera(
    host: str = "192.168.1.100",
    port: int = 80,
    user: str | None = "admin",
    passwd: str | None = "password",  # noqa: S107
    wsdl_dir: str = _REAL_WSDL_DIR,
) -> AsyncGenerator[ONVIFCamera]:
    """Create a test camera instance with context manager."""
    cam = ONVIFCamera(host, port, user, passwd, wsdl_dir=wsdl_dir)
    try:
        yield cam
    finally:
        await cam.close()


# --------------------------------------------------------------------------
# _list_wsdl_dir
# --------------------------------------------------------------------------


def test_bundled_wsdl_dir_is_prewarmed_at_import() -> None:
    """Import-time pre-warm populates _WSDL_DIR_FILES for the bundled wsdl dir."""
    cached = _WSDL_DIR_FILES.get(_WSDL_PATH)
    assert cached is not None
    assert "devicemgmt.wsdl" in cached


def test_list_wsdl_dir_returns_regular_files() -> None:
    """The bundled WSDL directory yields the wsdl file names as a set."""
    files = _list_wsdl_dir(_REAL_WSDL_DIR)
    assert files is not None
    assert "devicemgmt.wsdl" in files


def test_list_wsdl_dir_missing_returns_empty() -> None:
    """A nonexistent directory yields an empty set so lookups fail cleanly."""
    assert _list_wsdl_dir("/definitely/not/a/real/wsdl/dir") == frozenset()


def test_list_wsdl_dir_unreadable_returns_none(tmp_path) -> None:
    """A directory that scandir cannot read returns None to trigger fallback."""

    msg = "blocked"

    def _raise_permission(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(msg)

    with patch("onvif.client.os.scandir", _raise_permission):
        assert _list_wsdl_dir(str(tmp_path)) is None


# --------------------------------------------------------------------------
# _get_shared_sqlite_cache
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shared_sqlite_cache_is_singleton() -> None:
    """Repeat calls return the same instance without rebuilding the cache."""
    with patch("onvif.client.SqliteCache") as cache_cls:
        cache_cls.return_value = Mock()
        first = await _get_shared_sqlite_cache()
        second = await _get_shared_sqlite_cache()

    assert first is second
    assert cache_cls.call_count == 1


# --------------------------------------------------------------------------
# ZeepAsyncClient.create_service
# --------------------------------------------------------------------------


def test_create_service_unknown_binding_raises_value_error() -> None:
    """create_service raises ValueError when the binding QName is unknown."""
    client = ZeepAsyncClient.__new__(ZeepAsyncClient)
    client.wsdl = Mock()
    client.wsdl.bindings = {}

    with pytest.raises(ValueError, match="No binding found"):
        client.create_service("{ns}Missing", "http://example.com")


# --------------------------------------------------------------------------
# ONVIFService
# --------------------------------------------------------------------------


def test_onvif_service_missing_wsdl_raises() -> None:
    """Constructing a service with a missing WSDL file raises ONVIFError."""
    with pytest.raises(ONVIFError):
        ONVIFService(
            "http://example.com",
            "admin",
            "password",
            "/nonexistent/does-not-exist.wsdl",
        )


@pytest.mark.asyncio
async def test_setup_attaches_shared_sqlite_cache_when_caching() -> None:
    """Two services with no_cache=False end up pointing at the same cache.

    Aborts setup() right after the cache assignment so the test does not need
    to mock the rest of zeep's binding/namespace plumbing.
    """
    wsdl = str(Path(_REAL_WSDL_DIR) / "devicemgmt.wsdl")
    fake_cache = Mock()
    stop = RuntimeError("stop after cache assignment")

    service_a = ONVIFService("http://a/", "u", "p", wsdl, no_cache=False)
    service_b = ONVIFService("http://b/", "u", "p", wsdl, no_cache=False)
    try:
        assert service_a.transport.cache is None
        assert service_b.transport.cache is None

        with (
            patch(
                "onvif.client._get_shared_sqlite_cache",
                new=AsyncMock(return_value=fake_cache),
            ),
            patch("onvif.client._cached_document", new=AsyncMock(side_effect=stop)),
        ):
            with pytest.raises(RuntimeError):
                await service_a.setup()
            with pytest.raises(RuntimeError):
                await service_b.setup()

        assert service_a.transport.cache is fake_cache
        assert service_b.transport.cache is fake_cache
    finally:
        await service_a.close()
        await service_b.close()


@pytest.mark.asyncio
async def test_setup_leaves_cache_none_when_no_cache_is_true() -> None:
    """no_cache=True must not pull in the shared SqliteCache during setup()."""
    wsdl = str(Path(_REAL_WSDL_DIR) / "devicemgmt.wsdl")
    stop = RuntimeError("stop after cache check")
    service = ONVIFService("http://a/", "u", "p", wsdl, no_cache=True)
    try:
        get_cache = AsyncMock()
        with (
            patch("onvif.client._get_shared_sqlite_cache", new=get_cache),
            patch("onvif.client._cached_document", new=AsyncMock(side_effect=stop)),
            pytest.raises(RuntimeError),
        ):
            await service.setup()

        assert service.transport.cache is None
        get_cache.assert_not_called()
    finally:
        await service.close()


def test_service_wrapper_falls_back_to_positional_args() -> None:
    """A service op that rejects keyword args is retried with positional args."""
    service = ONVIFService.__new__(ONVIFService)

    def operation(*args, **kwargs):
        if kwargs:
            msg = "keyword arguments not accepted"
            raise TypeError(msg)
        return ("positional", args)

    ws_client = Mock()
    ws_client.SomeOperation = operation
    service.ws_client = ws_client

    result = service.SomeOperation({"Foo": "bar"})

    assert result == ("positional", ({"Foo": "bar"},))


def test_service_wrapper_no_params() -> None:
    """Calling a service op with no params invokes the underlying op with none."""
    service = ONVIFService.__new__(ONVIFService)
    ws_client = Mock()
    ws_client.GetThing = Mock(return_value="ok")
    service.ws_client = ws_client

    result = service.GetThing()

    assert result == "ok"
    ws_client.GetThing.assert_called_once_with()


def test_service_wrapper_does_not_swallow_non_typeerror() -> None:
    """A non-TypeError raised by the underlying op surfaces as ONVIFError.

    The keyword/positional fallback must only react to TypeError, otherwise
    legitimate signature-unrelated failures would be silently retried with
    positional args and produce confusing downstream errors.
    """
    service = ONVIFService.__new__(ONVIFService)
    msg = "underlying failure"

    def operation(**_kwargs):
        raise ValueError(msg)

    ws_client = Mock()
    ws_client.BrokenOp = operation
    service.ws_client = ws_client

    with pytest.raises(ONVIFError) as excinfo:
        service.BrokenOp({"Foo": "bar"})
    assert isinstance(excinfo.value.__cause__, ValueError)


def test_getattr_unknown_dunder_raises_key_error() -> None:
    """Accessing an unset dunder attribute raises KeyError (not a wrapper)."""
    service = ONVIFService.__new__(ONVIFService)
    with pytest.raises(KeyError):
        service.__not_a_real_dunder__  # noqa: B018


def test_authless_dispatch_preserves_underscores_in_method_name() -> None:
    """authless_<Method> must dispatch to the full suffix, not the chunk before the next underscore.

    Regression: the previous implementation used name.split("_")[1], which
    silently dropped everything after the first underscore in the operation
    name. Any future authless call whose ONVIF op name contains an underscore
    would resolve to the wrong attribute on ws_client_authless.
    """
    service = ONVIFService.__new__(ONVIFService)
    ws_client_authless = Mock()
    ws_client_authless.Get_Some_Method = Mock(return_value="full-name")
    ws_client_authless.Get = Mock(return_value="first-chunk")
    service.ws_client_authless = ws_client_authless

    result = service.authless_Get_Some_Method()

    assert result == "full-name"
    ws_client_authless.Get_Some_Method.assert_called_once_with()
    ws_client_authless.Get.assert_not_called()


def test_authless_dispatch_existing_pascalcase_method_still_works() -> None:
    """The common case (PascalCase, no underscores) continues to dispatch correctly."""
    service = ONVIFService.__new__(ONVIFService)
    ws_client_authless = Mock()
    ws_client_authless.GetSystemDateAndTime = Mock(return_value="ok")
    service.ws_client_authless = ws_client_authless

    result = service.authless_GetSystemDateAndTime()

    assert result == "ok"
    ws_client_authless.GetSystemDateAndTime.assert_called_once_with()


# --------------------------------------------------------------------------
# ONVIFCamera.has_broken_relative_time
# --------------------------------------------------------------------------


def _make_bare_camera() -> ONVIFCamera:
    """Build an ONVIFCamera with only the attributes the pure methods touch."""
    cam = ONVIFCamera.__new__(ONVIFCamera)
    cam.host = "1.2.3.4"
    cam.port = 80
    cam.wsdl_dir = _REAL_WSDL_DIR
    cam.xaddrs = {}
    cam.dt_diff = None
    cam._has_broken_relative_timestamps = False
    return cam


def _utc(*args: int) -> dt.datetime:
    """Build a timezone-aware UTC datetime."""
    return dt.datetime(*args, tzinfo=dt.timezone.utc)


def _naive(*args: int) -> dt.datetime:
    """Build a naive datetime (no timezone info)."""
    return _utc(*args).replace(tzinfo=None)


def test_has_broken_relative_time_no_current_time() -> None:
    """Returns False when the device reports no current time."""
    cam = _make_bare_camera()
    assert (
        cam.has_broken_relative_time(dt.timedelta(seconds=60), None, _utc(2024, 1, 1))
        is False
    )


def test_has_broken_relative_time_no_termination_time() -> None:
    """Returns False when the device reports no termination time."""
    cam = _make_bare_camera()
    assert (
        cam.has_broken_relative_time(dt.timedelta(seconds=60), _utc(2024, 1, 1), None)
        is False
    )


def test_has_broken_relative_time_current_time_naive() -> None:
    """Returns False when the current time has no timezone info."""
    cam = _make_bare_camera()
    assert (
        cam.has_broken_relative_time(
            dt.timedelta(seconds=60), _naive(2024, 1, 1), _utc(2024, 1, 1)
        )
        is False
    )


def test_has_broken_relative_time_termination_time_naive() -> None:
    """Returns False when the termination time has no timezone info."""
    cam = _make_bare_camera()
    assert (
        cam.has_broken_relative_time(
            dt.timedelta(seconds=60), _utc(2024, 1, 1), _naive(2024, 1, 1)
        )
        is False
    )


def test_has_broken_relative_time_detected() -> None:
    """A too-short actual interval flags broken timestamps and returns True."""
    cam = _make_bare_camera()
    current = _utc(2024, 1, 1, 0, 0, 0)
    termination = _utc(2024, 1, 1, 0, 0, 10)
    result = cam.has_broken_relative_time(
        dt.timedelta(seconds=60), current, termination
    )
    assert result is True
    assert cam._has_broken_relative_timestamps is True


def test_has_broken_relative_time_ok() -> None:
    """A correct interval keeps timestamps relative and returns False."""
    cam = _make_bare_camera()
    current = _utc(2024, 1, 1, 0, 0, 0)
    termination = _utc(2024, 1, 1, 0, 1, 0)
    result = cam.has_broken_relative_time(
        dt.timedelta(seconds=60), current, termination
    )
    assert result is False
    assert cam._has_broken_relative_timestamps is False


# --------------------------------------------------------------------------
# ONVIFCamera.get_next_termination_time
# --------------------------------------------------------------------------


def test_get_next_termination_time_relative() -> None:
    """Default (non-broken) timestamps yield an ISO 8601 duration."""
    cam = _make_bare_camera()
    assert cam.get_next_termination_time(dt.timedelta(seconds=90)) == "PT90S"


def test_get_next_termination_time_absolute_with_dt_diff() -> None:
    """Broken timestamps yield a Zulu absolute time including dt_diff offset."""
    cam = _make_bare_camera()
    cam._has_broken_relative_timestamps = True
    cam.dt_diff = dt.timedelta(seconds=5)

    fixed_now = dt.datetime(2024, 1, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
    with patch("onvif.client.utcnow", return_value=fixed_now):
        result = cam.get_next_termination_time(dt.timedelta(seconds=60))

    # 00:00:00 + 60s duration + 5s dt_diff = 00:01:05, formatted as Zulu.
    assert result == "2024-01-01T00:01:05Z"


# --------------------------------------------------------------------------
# Manager factories
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_pullpoint_manager() -> None:
    """create_pullpoint_manager builds a manager and starts it."""
    async with create_test_camera() as cam:
        with patch("onvif.client.PullPointManager") as mock_manager:
            instance = mock_manager.return_value
            instance.start = AsyncMock()
            callback = Mock()
            interval = dt.timedelta(seconds=60)

            result = await cam.create_pullpoint_manager(interval, callback)

            assert result is instance
            mock_manager.assert_called_once_with(cam, interval, callback)
            instance.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_notification_manager() -> None:
    """create_notification_manager builds a manager and starts it."""
    async with create_test_camera() as cam:
        with patch("onvif.client.NotificationManager") as mock_manager:
            instance = mock_manager.return_value
            instance.start = AsyncMock()
            callback = Mock()
            interval = dt.timedelta(seconds=60)

            result = await cam.create_notification_manager(
                "http://example.com/notify", interval, callback
            )

            assert result is instance
            mock_manager.assert_called_once_with(
                cam, "http://example.com/notify", interval, callback
            )
            instance.start.assert_awaited_once()


# --------------------------------------------------------------------------
# ONVIFCamera.get_definition
# --------------------------------------------------------------------------


def test_get_definition_unknown_service() -> None:
    """An unknown service name raises ONVIFError."""
    cam = _make_bare_camera()
    with pytest.raises(ONVIFError, match="Unknown service"):
        cam.get_definition("not_a_service")


def test_get_definition_missing_wsdl_file() -> None:
    """A service whose WSDL file is absent raises ONVIFError."""
    cam = _make_bare_camera()
    cam.wsdl_dir = "/nonexistent-wsdl-dir"
    with pytest.raises(ONVIFError, match="No such file"):
        cam.get_definition("media")


def test_get_definition_devicemgmt_fixed_xaddr() -> None:
    """devicemgmt resolves to the fixed /onvif/device_service xaddr."""
    cam = _make_bare_camera()
    xaddr, wsdlpath, binding_name = cam.get_definition("devicemgmt")
    assert xaddr == "http://1.2.3.4:80/onvif/device_service"
    assert wsdlpath.endswith("devicemgmt.wsdl")
    assert "DeviceBinding" in binding_name


def test_get_definition_with_port_type() -> None:
    """A port_type is appended to the namespace used for the xaddr lookup."""
    cam = _make_bare_camera()
    namespace = "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription"
    cam.xaddrs[namespace] = "http://1.2.3.4/onvif/pullpoint"

    xaddr, wsdlpath, _binding_name = cam.get_definition(
        "pullpoint", port_type="PullPointSubscription"
    )

    assert xaddr == "http://1.2.3.4/onvif/pullpoint"
    assert wsdlpath.endswith("events.wsdl")


def test_get_definition_unsupported_service_without_xaddr() -> None:
    """A known service with no discovered xaddr raises ONVIFError."""
    cam = _make_bare_camera()
    with pytest.raises(ONVIFError, match="doesn`t support service"):
        cam.get_definition("media")


# --------------------------------------------------------------------------
# ONVIFCamera.create_onvif_service
# --------------------------------------------------------------------------


@pytest.fixture
def _wsdl_scratch_dir(tmp_path: Path) -> None:
    """A scratch wsdl_dir prepared synchronously so async tests can use it."""
    (tmp_path / "devicemgmt.wsdl").write_text("<wsdl/>")


@pytest.mark.asyncio
@pytest.mark.usefixtures("_wsdl_scratch_dir")
async def test_create_onvif_service_warms_wsdl_dir_cache(tmp_path: Path) -> None:
    """A previously unseen wsdl_dir is scanned off the event loop on first use."""
    wsdl_dir = str(tmp_path)
    assert wsdl_dir not in _WSDL_DIR_FILES

    async with create_test_camera(wsdl_dir=wsdl_dir) as cam:
        sentinel = Mock(spec=ONVIFService)
        sentinel.setup = AsyncMock()
        with patch("onvif.client.ONVIFService", return_value=sentinel):
            await cam.create_onvif_service("devicemgmt")

    cached = _WSDL_DIR_FILES.get(wsdl_dir)
    assert cached is not None
    assert "devicemgmt.wsdl" in cached


@pytest.mark.asyncio
async def test_create_onvif_service_returns_cached_when_xaddr_unchanged() -> None:
    """An existing service with the same xaddr is reused, not recreated."""
    async with create_test_camera() as cam:
        existing = Mock()
        existing.xaddr = "http://1.2.3.4/onvif/media"
        existing.close = AsyncMock()
        cam.services[("media", None)] = existing

        with patch.object(
            cam,
            "get_definition",
            return_value=("http://1.2.3.4/onvif/media", "media.wsdl", "{ns}Binding"),
        ):
            result = await cam.create_onvif_service("media")

        assert result is existing


@pytest.mark.asyncio
async def test_create_onvif_service_recreates_when_xaddr_changes() -> None:
    """An existing service with a stale xaddr is closed and recreated."""
    async with create_test_camera() as cam:
        existing = Mock()
        existing.xaddr = "http://old-address/onvif/media"
        existing.close = AsyncMock()
        cam.services[("media", None)] = existing

        new_service = Mock()
        new_service.setup = AsyncMock()
        new_service.close = AsyncMock()

        with (
            patch.object(
                cam,
                "get_definition",
                return_value=(
                    "http://new-address/onvif/media",
                    "media.wsdl",
                    "{ns}Binding",
                ),
            ),
            patch("onvif.client.ONVIFService", return_value=new_service),
        ):
            result = await cam.create_onvif_service("media")

        existing.close.assert_awaited_once()
        new_service.setup.assert_awaited_once()
        assert result is new_service
        assert cam.services[("media", None)] is new_service


# --------------------------------------------------------------------------
# Per-service creation helpers
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "expected_args", "expected_kwargs"),
    [
        ("create_devicemgmt_service", ("devicemgmt",), {}),
        ("create_media_service", ("media",), {}),
        ("create_ptz_service", ("ptz",), {}),
        ("create_imaging_service", ("imaging",), {}),
        ("create_deviceio_service", ("deviceio",), {}),
        ("create_events_service", ("events",), {}),
        ("create_analytics_service", ("analytics",), {}),
        ("create_recording_service", ("recording",), {}),
        ("create_search_service", ("search",), {}),
        ("create_replay_service", ("replay",), {}),
        ("create_notification_service", ("notification",), {}),
        ("create_receiver_service", ("receiver",), {}),
    ],
)
async def test_create_service_helpers(
    method_name: str,
    expected_args: tuple,
    expected_kwargs: dict,
) -> None:
    """Each create_*_service helper delegates to create_onvif_service."""
    async with create_test_camera() as cam:
        sentinel = Mock()
        with patch.object(
            cam, "create_onvif_service", new=AsyncMock(return_value=sentinel)
        ) as mock_create:
            result = await getattr(cam, method_name)()

        assert result is sentinel
        mock_create.assert_awaited_once_with(*expected_args, **expected_kwargs)


@pytest.mark.asyncio
async def test_create_pullpoint_service_uses_pullpoint_timeouts() -> None:
    """create_pullpoint_service passes the pullpoint port type and timeouts."""
    async with create_test_camera() as cam:
        sentinel = Mock()
        with patch.object(
            cam, "create_onvif_service", new=AsyncMock(return_value=sentinel)
        ) as mock_create:
            result = await cam.create_pullpoint_service()

        assert result is sentinel
        mock_create.assert_awaited_once_with(
            "pullpoint",
            port_type="PullPointSubscription",
            read_timeout=onvif.client._PULLPOINT_TIMEOUT,
            write_timeout=onvif.client._PULLPOINT_TIMEOUT,
        )


@pytest.mark.asyncio
async def test_create_subscription_service_passes_port_type() -> None:
    """create_subscription_service forwards the optional port_type."""
    async with create_test_camera() as cam:
        sentinel = Mock()
        with patch.object(
            cam, "create_onvif_service", new=AsyncMock(return_value=sentinel)
        ) as mock_create:
            result = await cam.create_subscription_service("SomePortType")

        assert result is sentinel
        mock_create.assert_awaited_once_with("subscription", port_type="SomePortType")


# --------------------------------------------------------------------------
# safe_func: exception passthrough
# --------------------------------------------------------------------------


def test_safe_func_passes_through_onvif_timeout_error() -> None:
    """safe_func must not downgrade ONVIFTimeoutError to base ONVIFError.

    Callers (e.g. Home Assistant's onvif integration) branch on the subclass
    to decide whether to retry. Wrapping it in ONVIFError destroys that
    contract.
    """

    msg = "camera unresponsive"

    @onvif.client.safe_func
    def raises_timeout() -> None:
        raise ONVIFTimeoutError(msg)

    with pytest.raises(ONVIFTimeoutError):
        raises_timeout()


def test_safe_func_passes_through_onvif_auth_error() -> None:
    """safe_func must not downgrade ONVIFAuthError to base ONVIFError."""

    msg = "bad credentials"

    @onvif.client.safe_func
    def raises_auth() -> None:
        raise ONVIFAuthError(msg)

    with pytest.raises(ONVIFAuthError):
        raises_auth()


def test_safe_func_passes_through_base_onvif_error_unchanged() -> None:
    """safe_func must not double-wrap an ONVIFError into another ONVIFError."""
    original = ONVIFError("explicit failure")

    @onvif.client.safe_func
    def raises_onvif() -> None:
        raise original

    with pytest.raises(ONVIFError) as excinfo:
        raises_onvif()
    # The original exception instance must propagate, not a fresh wrapper.
    assert excinfo.value is original


def test_safe_func_still_wraps_generic_exceptions() -> None:
    """safe_func should still convert non-ONVIF exceptions into ONVIFError."""

    msg = "oops"

    @onvif.client.safe_func
    def raises_value_error() -> None:
        raise ValueError(msg)

    with pytest.raises(ONVIFError) as excinfo:
        raises_value_error()
    assert not isinstance(excinfo.value, (ONVIFTimeoutError, ONVIFAuthError))
    assert isinstance(excinfo.value.__cause__, ValueError)
