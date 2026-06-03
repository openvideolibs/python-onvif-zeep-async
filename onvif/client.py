"""ONVIF Client."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os.path
from pathlib import Path
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

import aiohttp
import zeep.helpers
from aiohttp import BasicAuth, ClientSession, DigestAuthMiddleware, TCPConnector
from zeep.cache import SqliteCache
from zeep.client import AsyncClient as BaseZeepAsyncClient
from zeep.proxy import AsyncServiceProxy
from zeep.wsdl import Document
from zeep.wsse.username import UsernameToken

from onvif.definition import SERVICES
from onvif.exceptions import ONVIFAuthError, ONVIFError, ONVIFTimeoutError

from .const import KEEPALIVE_EXPIRY
from .managers import NotificationManager, PullPointManager
from .settings import DEFAULT_SETTINGS
from .transport import ASYNC_TRANSPORT
from .types import FastDateTime, ForgivingTime
from .util import (
    create_no_verify_ssl_context,
    normalize_url,
    obscure_user_pass_url,
    path_isfile,
    replace_host_port,
    strip_user_pass_url,
    utcnow,
)
from .wrappers import retry_connection_error
from .wsa import WsAddressingIfMissingPlugin
from .zeep_aiohttp import AIOHTTPTransport

if TYPE_CHECKING:
    from collections.abc import Callable

    import httpx
    from requests import Response

logger = logging.getLogger("onvif")
logging.basicConfig(level=logging.INFO)
logging.getLogger("zeep.client").setLevel(logging.CRITICAL)

_SENTINEL = object()
# Default wsdl_dir for ONVIFCamera. Points at the WSDL files bundled with the
# package (onvif/wsdl); historically this was off by one level and pointed at a
# directory that did not exist, which silently forced every caller to override.
_WSDL_PATH = str(Path(__file__).parent / "wsdl")
# Names of regular files in each wsdl_dir, populated lazily off the event loop
# on first use so the directory scan stays out of the asyncio path. None means
# the cache could not be built and callers should fall back to path_isfile.
_WSDL_DIR_FILES: dict[str, frozenset[str] | None] = {}


def _list_wsdl_dir(wsdl_dir: str) -> frozenset[str] | None:
    """Return the set of regular file names in wsdl_dir.

    Returns an empty frozenset if the directory itself does not exist (then
    every wsdl lookup correctly fails); returns None on any other OSError so
    callers fall back to path_isfile rather than treating a permissions or
    I/O failure as "wsdl not found".
    """
    try:
        with os.scandir(wsdl_dir) as it:
            return frozenset(entry.name for entry in it if entry.is_file())
    except FileNotFoundError:
        return frozenset()
    except OSError:
        return None


# Pre-warm the bundled wsdl directory at module import time so direct
# ONVIFService(...) usage in async code with a bundled wsdl path does not trip
# blockbuster on the first existence check. Import normally happens at startup
# before any event loop is running; if onvif.client is imported from inside a
# running loop, the alternative (lazy warm on first direct use) would block in
# the loop too, so accept the one-shot scandir here.
_WSDL_DIR_FILES[_WSDL_PATH] = _list_wsdl_dir(_WSDL_PATH)


# SqliteCache opens its sqlite file in __init__, which does blocking I/O.
# Build one shared instance lazily off the event loop and reuse it across all
# ONVIFService transports so the per-service setup() stays non-blocking. The
# Lock is created lazily on first use (we cannot promise a running loop at
# import time) and double-checked, so concurrent first-touch setup() calls
# (for example, multiple cameras configured in parallel at startup) build
# exactly one SqliteCache instead of racing through to_thread() twice. The
# pre-Lock check-and-create is race-free because asyncio coroutines are
# cooperatively scheduled within a single loop and there is no await between
# the None check and the assignment.
_SHARED_SQLITE_CACHE: SqliteCache | None = None
_SHARED_SQLITE_CACHE_LOCK: asyncio.Lock | None = None


async def _get_shared_sqlite_cache() -> SqliteCache:
    global _SHARED_SQLITE_CACHE, _SHARED_SQLITE_CACHE_LOCK  # noqa: PLW0603
    if _SHARED_SQLITE_CACHE is None:
        if _SHARED_SQLITE_CACHE_LOCK is None:
            _SHARED_SQLITE_CACHE_LOCK = asyncio.Lock()
        async with _SHARED_SQLITE_CACHE_LOCK:
            if _SHARED_SQLITE_CACHE is None:
                _SHARED_SQLITE_CACHE = await asyncio.to_thread(SqliteCache)
    return _SHARED_SQLITE_CACHE


_DEFAULT_TIMEOUT = 90
_PULLPOINT_TIMEOUT = 90
_CONNECT_TIMEOUT = 30
_READ_TIMEOUT = 90
_WRITE_TIMEOUT = 90
# Keepalive is set on the connector, not in ClientTimeout
_NO_VERIFY_SSL_CONTEXT = create_no_verify_ssl_context()


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _resolve_active_prefix(namespaces: dict[str, str], namespace: str) -> str:
    """Return the prefix bound to *namespace* in *namespaces*.

    Falls back to ``"ns0"`` when the namespace is not present or is bound to an
    empty prefix. The single-pass ``next()`` form replaces an older
    ``list(keys)[list(values).index(...)]`` lookup that built two parallel lists
    and raised ``ValueError`` on a miss instead of using the documented fallback.
    """
    return (
        next(
            (prefix for prefix, uri in namespaces.items() if uri == namespace),
            "",
        )
        or "ns0"
    )


def safe_func(func: Callable[_P, _R]) -> Callable[_P, _R]:
    """Ensure methods to raise an ONVIFError Exception when some thing was wrong.

    ONVIFError (and subclasses like ONVIFTimeoutError / ONVIFAuthError) are
    re-raised unchanged so callers can branch on the specific subtype.
    """

    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return func(*args, **kwargs)
        except ONVIFError:
            raise
        except Exception as err:
            raise ONVIFError(err) from err

    return wrapped


class UsernameDigestTokenDtDiff(UsernameToken):
    """
    UsernameDigestToken class, with a time offset parameter that can be adjusted;
    This allows authentication on cameras without being time synchronized.
    Please note that using NTP on both end is the recommended solution,
    this should only be used in "safe" environments.
    """

    def __init__(self, user, passw, dt_diff=None, **kwargs):
        # ONVIF / WS-Security UsernameToken Profile requires the Created
        # timestamp (and the timestamp folded into the password digest) to be
        # in canonical UTC "Zulu" form, e.g. 2024-01-01T00:00:00Z. zeep emits a
        # numeric "+00:00" offset by default, which some camera firmwares
        # (notably Hikvision) reject, causing digest auth to fail. Default to
        # Zulu timestamps unless the caller explicitly overrides.
        kwargs.setdefault("zulu_timestamp", True)
        super().__init__(user, passw, **kwargs)
        # Date/time difference in datetime.timedelta
        self.dt_diff = dt_diff

    def apply(self, envelope, headers):
        old_created = self.created
        if self.created is None:
            self.created = dt.datetime.now(tz=dt.timezone.utc).replace(tzinfo=None)
        if self.dt_diff is not None:
            self.created += self.dt_diff
        result = super().apply(envelope, headers)
        self.created = old_created
        return result


_DOCUMENT_CACHE: dict[str, Document] = {}

original_load = Document.load


class DocumentWithDeferredLoad(Document):
    def load(self, *args: Any, **kwargs: Any) -> None:
        """Deferred load of the document."""

    def original_load(self, *args: Any, **kwargs: Any) -> None:
        """Original load of the document."""
        return original_load(self, *args, **kwargs)


class AsyncTransportProtocolErrorHandler(AIOHTTPTransport):
    """
    Retry on remote protocol error.

    http://datatracker.ietf.org/doc/html/rfc2616#section-8.1.4 allows the server
    to close the connection at any time, we treat this as normal and try again
    once. Two flavors of "the pooled socket is dead before we wrote":
    ServerDisconnectedError when aiohttp detects the close at request start, and
    ClientConnectionResetError when it detects the close mid-prepare (the writer
    raises before transport.write()). Both mean the request did not reach the
    server, so retry is idempotency-safe; we deliberately do not catch the
    broader ClientOSError because that can fire after bytes are on the wire.
    """

    @retry_connection_error(
        attempts=2,
        exception=(
            aiohttp.ServerDisconnectedError,
            aiohttp.ClientConnectionResetError,
        ),
        backoff=0,
    )
    async def post(
        self, address: str, message: str, headers: dict[str, str]
    ) -> httpx.Response:
        return await super().post(address, message, headers)

    @retry_connection_error(
        attempts=2,
        exception=(
            aiohttp.ServerDisconnectedError,
            aiohttp.ClientConnectionResetError,
        ),
        backoff=0,
    )
    async def get(
        self,
        address: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        return await super().get(address, params, headers)

    @retry_connection_error(
        attempts=2,
        exception=(
            aiohttp.ServerDisconnectedError,
            aiohttp.ClientConnectionResetError,
        ),
        backoff=0,
    )
    async def post_xml(
        self, address: str, envelope: Any, headers: dict[str, str]
    ) -> Response:
        return await super().post_xml(address, envelope, headers)


async def _cached_document(url: str) -> Document:
    """Load external XML document from disk."""
    if url in _DOCUMENT_CACHE:
        return _DOCUMENT_CACHE[url]
    loop = asyncio.get_running_loop()

    def _load_document() -> DocumentWithDeferredLoad:
        document = DocumentWithDeferredLoad(
            url, ASYNC_TRANSPORT, settings=DEFAULT_SETTINGS
        )
        # Override the default datetime type to use FastDateTime
        # This is a workaround for the following issue:
        # https://github.com/mvantellingen/python-zeep/pull/1370
        schema = document.types.documents.get_by_namespace(
            "http://www.w3.org/2001/XMLSchema", False
        )[0]
        logger.debug("Overriding default datetime type to use FastDateTime")
        instance = FastDateTime(is_global=True)
        schema.register_type(FastDateTime._default_qname, instance)

        logger.debug("Overriding default time type to use ForgivingTime")
        instance = ForgivingTime(is_global=True)
        schema.register_type(ForgivingTime._default_qname, instance)

        document.types.add_documents([None], url)
        # Perform the original load
        document.original_load(url)
        return document

    document = await loop.run_in_executor(None, _load_document)
    _DOCUMENT_CACHE[url] = document
    return document


_T = TypeVar("_T")


def handle_snapshot_errors(func: Callable[..., _T]) -> Callable[..., _T]:
    """Decorator to handle snapshot URI errors."""

    async def wrapper(self, uri: str, *args: Any, **kwargs: Any) -> _T:
        try:
            return await func(self, uri, *args, **kwargs)
        except TimeoutError as error:
            msg = f"Timed out fetching {obscure_user_pass_url(uri)}: {error}"
            raise ONVIFTimeoutError(msg) from error
        except aiohttp.ClientError as error:
            msg = f"Error fetching {obscure_user_pass_url(uri)}: {error}"
            raise ONVIFError(msg) from error

    return wrapper


class ZeepAsyncClient(BaseZeepAsyncClient):
    """Overwrite create_service method to be async."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_ns_prefix("wsnt", "http://docs.oasis-open.org/wsn/b-2")
        self.set_ns_prefix("wsa", "http://www.w3.org/2005/08/addressing")

    def create_service(self, binding_name, address):
        """
        Create a new ServiceProxy for the given binding name and address.
        :param binding_name: The QName of the binding
        :param address: The address of the endpoint
        """
        try:
            binding = self.wsdl.bindings[binding_name]
        except KeyError:
            msg = (
                f"No binding found with the given QName. Available bindings "
                f"are: {', '.join(self.wsdl.bindings.keys())}"
            )
            raise ValueError(msg) from None
        return AsyncServiceProxy(self, binding, address=address)


class ONVIFService:
    """
    Python Implemention for ONVIF Service.
    Services List:
        DeviceMgmt DeviceIO Event AnalyticsDevice Display Imaging Media
        PTZ Receiver RemoteDiscovery Recording Replay Search Extension

    >>> from onvif import ONVIFService
    >>> device_service = ONVIFService(
    ...     'http://192.168.0.112/onvif/device_service',
    ...     'admin', 'foscam',
    ...     '/path/to/wsdl/devicemgmt.wsdl',
    ... )
    >>> ret = await device_service.GetHostname()
    >>> print(ret.FromDHCP)
    >>> print(ret.Name)
    >>> await device_service.SetHostname(dict(Name='newhostname'))
    >>> ret = await device_service.GetSystemDateAndTime()
    >>> print(ret.DaylightSavings)
    >>> print(ret.TimeZone)
    >>> dict_ret = device_service.to_dict(ret)
    >>> print(dict_ret['TimeZone'])

    There are two ways to pass parameter to services methods
    1. Dict
        params = {'Name': 'NewHostName'}
        device_service.SetHostname(params)
    2. Type Instance
        params = device_service.create_type('SetHostname')
        params.Hostname = 'NewHostName'
        device_service.SetHostname(params)
    """

    @safe_func
    def __init__(
        self,
        xaddr: str,
        user: str | None,
        passwd: str | None,
        url: str,
        encrypt=True,
        no_cache=False,
        dt_diff=None,
        binding_name="",
        binding_key="",
        read_timeout: int | None = None,
        write_timeout: int | None = None,
    ) -> None:
        wsdl_dir, wsdl_name = os.path.split(url)
        cached_files = _WSDL_DIR_FILES.get(wsdl_dir)
        exists = (
            wsdl_name in cached_files if cached_files is not None else path_isfile(url)
        )
        if not exists:
            msg = f"{url} doesn`t exist!"
            raise ONVIFError(msg)

        self.url = url
        self.xaddr = xaddr
        self.binding_key = binding_key
        # Set soap header for authentication
        self.user = user
        self.passwd = passwd
        # Indicate wether password digest is needed
        self.encrypt = encrypt
        self.dt_diff = dt_diff
        self.binding_name = binding_name
        # Create soap client
        self._connector = TCPConnector(
            ssl=_NO_VERIFY_SSL_CONTEXT,
            keepalive_timeout=KEEPALIVE_EXPIRY,
        )
        self._session = ClientSession(
            connector=self._connector,
            timeout=aiohttp.ClientTimeout(
                total=_DEFAULT_TIMEOUT,
                connect=_CONNECT_TIMEOUT,
                sock_read=read_timeout or _READ_TIMEOUT,
            ),
        )
        # Always use the retry-on-disconnect transport. RFC 2616 section 8.1.4
        # allows the server to close the connection at any time, and cameras
        # routinely do so between event polls. The cache only affects WSDL
        # loading, so it is orthogonal to the connection-error retry.
        # The SqliteCache opens a sqlite file in its constructor (blocking I/O);
        # leave cache=None here and let setup() attach the shared cache instance
        # built off the event loop.
        self._no_cache = no_cache
        self.transport = AsyncTransportProtocolErrorHandler(
            session=self._session,
            verify_ssl=False,
            cache=None,
        )
        self.document: Document | None = None
        self.zeep_client_authless: ZeepAsyncClient | None = None
        self.ws_client_authless: AsyncServiceProxy | None = None
        self.zeep_client: ZeepAsyncClient | None = None
        self.ws_client: AsyncServiceProxy | None = None
        self.create_type: Callable | None = None

    async def setup(self):
        """Setup the transport."""
        settings = DEFAULT_SETTINGS
        binding_name = self.binding_name
        wsse = UsernameDigestTokenDtDiff(
            self.user, self.passwd, dt_diff=self.dt_diff, use_digest=self.encrypt
        )
        if not self._no_cache and self.transport.cache is None:
            self.transport.cache = await _get_shared_sqlite_cache()
        self.document = await _cached_document(self.url)
        self.zeep_client_authless = ZeepAsyncClient(
            wsdl=self.document,
            transport=self.transport,
            settings=settings,
            plugins=[WsAddressingIfMissingPlugin()],
        )
        self.ws_client_authless = self.zeep_client_authless.create_service(
            binding_name, self.xaddr
        )
        self.zeep_client = ZeepAsyncClient(
            wsdl=self.document,
            wsse=wsse,
            transport=self.transport,
            settings=settings,
            plugins=[WsAddressingIfMissingPlugin()],
        )
        self.ws_client = self.zeep_client.create_service(binding_name, self.xaddr)
        namespace = binding_name[binding_name.find("{") + 1 : binding_name.find("}")]
        active_ns = _resolve_active_prefix(self.zeep_client.namespaces, namespace)
        self.create_type = lambda x: self.zeep_client.get_element(f"{active_ns}:{x}")()

    async def close(self):
        """Close the transport."""
        await self.transport.aclose()
        await self._session.close()
        await self._connector.close()

    @staticmethod
    @safe_func
    def to_dict(zeepobject):
        """Convert a WSDL Type instance into a dictionary."""
        return {} if zeepobject is None else zeep.helpers.serialize_object(zeepobject)

    def __getattr__(self, name):
        """
        Call the real onvif Service operations,
        See the official wsdl definition for the
        APIs detail(API name, request parameters,
        response parameters, parameter types, etc...)
        """
        if name.startswith("__") and name.endswith("__"):
            return self.__dict__[name]
        if name.startswith("authless_"):
            target = self.ws_client_authless
            op_name = name.removeprefix("authless_")
        else:
            target = self.ws_client
            op_name = name
        func = getattr(target, op_name)

        @safe_func
        def wrapped(params=None):
            params = {} if params is None else ONVIFService.to_dict(params)
            try:
                return func(**params)
            except TypeError:
                return func(params)

        return wrapped


class ONVIFCamera:
    """
    Python Implementation ONVIF compliant device
    This class integrates onvif services

    adjust_time parameter allows authentication on cameras without being time synchronized.
    Please note that using NTP on both end is the recommended solution,
    this should only be used in "safe" environments.
    Also, this cannot be used on AXIS camera, as every request is authenticated, contrary to ONVIF standard

    >>> from onvif import ONVIFCamera
    >>> mycam = ONVIFCamera('192.168.0.112', 80, 'admin', '12345')
    >>> await mycam.update_xaddrs()
    >>> await mycam.devicemgmt.GetServices(False)
    >>> media_service = mycam.create_media_service()
    >>> ptz_service = mycam.create_ptz_service()
    # Get PTZ Configuration:
    >>> await mycam.ptz.GetConfiguration()
    # Another way:
    >>> await ptz_service.GetConfiguration()
    """

    def __init__(
        self,
        host: str,
        port: int,
        user: str | None,
        passwd: str | None,
        wsdl_dir: str = _WSDL_PATH,
        encrypt=True,
        no_cache=False,
        adjust_time=False,
        nat_override: bool = False,
    ) -> None:
        os.environ.pop("http_proxy", None)
        os.environ.pop("https_proxy", None)
        self.host = host
        self.port = int(port)
        self.user = user
        self.passwd = passwd
        self.wsdl_dir = wsdl_dir
        self.encrypt = encrypt
        self.no_cache = no_cache
        self.adjust_time = adjust_time
        # When True, URLs the device returns (XAddrs, subscription addresses,
        # snapshot URI) have their host:port rewritten to the host:port passed
        # to this constructor. Required for cameras behind NAT, which advertise
        # their LAN address in responses -- unreachable from outside the NAT.
        #
        # Assumes a single port-forward to the device: every advertised URL is
        # forced to the constructor host:port, so a camera that serves services
        # on separately-forwarded ports gets the wrong port for the others.
        # RTSP stream URIs (GetStreamUri on the media service) are not covered;
        # a NAT caller fetching the stream URI still gets the LAN address.
        self.nat_override = nat_override
        self.dt_diff = None
        self.xaddrs = {}
        self._has_broken_relative_timestamps: bool = False
        self._capabilities: dict[str, Any] | None = None

        # Active service client container
        self.services: dict[tuple[str, str | None], ONVIFService] = {}

        self.to_dict = ONVIFService.to_dict

        self._snapshot_uris = {}
        self._snapshot_connector = TCPConnector(ssl=_NO_VERIFY_SSL_CONTEXT)
        self._snapshot_client = ClientSession(connector=self._snapshot_connector)

    def rewrite_url(self, url: str | None) -> str | None:
        """Rewrite ``url`` to use this camera's host:port when nat_override is set.

        No-op when ``nat_override`` is disabled (default), so existing callers
        that connect on the LAN keep the device-advertised URL verbatim.

        Public because ``managers.py`` calls it across the class boundary to
        rewrite subscription reference addresses.
        """
        if not self.nat_override:
            return url
        return replace_host_port(url, self.host, self.port)

    async def get_capabilities(self) -> dict[str, Any] | None:
        """Get device capabilities.

        Returns the parsed GetCapabilities structure, or ``None`` if the device
        returned a payload that could not be serialized -- capabilities parsing
        is best-effort and swallows serialization errors (see
        ``_update_xaddrs_from_capabilities``).
        """
        if self._capabilities is None:
            # update_xaddrs() prefers GetServices, which does not return the
            # Category-keyed capabilities structure, so fetch GetCapabilities
            # here on demand to populate self._capabilities. _devicemgmt_with_time()
            # reproduces the adjust_time clock-skew compensation update_xaddrs()
            # performs, so a caller invoking get_capabilities() first on a
            # clock-skewed camera still signs GetCapabilities with an adjusted
            # timestamp.
            devicemgmt = await self._devicemgmt_with_time()
            await self._update_xaddrs_from_capabilities(devicemgmt)
        return self._capabilities

    async def update_xaddrs(self):
        """Update xaddrs for services."""
        self.dt_diff = None
        devicemgmt = await self._devicemgmt_with_time()

        # Get XAddr of services on the device.
        #
        # Prefer GetServices -- the ONVIF-recommended discovery method that
        # returns every service the device exposes (recording, replay, search,
        # receiver, deviceio, ...), not just the top-level subset advertised by
        # GetCapabilities. Fall back to GetCapabilities only when GetServices is
        # unsupported or returns nothing, which keeps older devices working and
        # avoids a redundant second round-trip on modern ones. self._capabilities
        # is populated lazily by get_capabilities() since GetServices does not
        # return the Category-keyed capabilities structure.
        # See https://github.com/openvideolibs/python-onvif-zeep-async/issues/97
        self.xaddrs = {}
        if not await self._update_xaddrs_from_services(devicemgmt):
            await self._update_xaddrs_from_capabilities(devicemgmt)

    async def _devicemgmt_with_time(self) -> ONVIFService:
        """Create the devicemgmt service with clock-skew compensation applied.

        Shared prologue for ``update_xaddrs()`` and ``get_capabilities()``: both
        need a devicemgmt service whose WS-Security timestamps account for the
        device clock offset. The ``adjust_time`` handshake runs only when
        ``dt_diff`` has not been computed yet, so calling this from
        ``get_capabilities()`` after ``update_xaddrs()`` does not repeat the
        round-trip. ``update_xaddrs()`` resets ``dt_diff`` to ``None`` first, so
        it always (re)runs the handshake. A no-op when ``adjust_time`` is
        disabled. Returns the devicemgmt service the caller should continue to use.
        """
        devicemgmt = await self.create_devicemgmt_service()
        if self.dt_diff is None:
            devicemgmt = await self._adjust_time(devicemgmt)
        return devicemgmt

    async def _adjust_time(self, devicemgmt: ONVIFService) -> ONVIFService:
        """Compute the device clock offset and recreate the devicemgmt service.

        When ``adjust_time`` is enabled, query the device's system clock and
        store its offset from the host clock in ``self.dt_diff`` so subsequent
        WS-Security timestamps compensate for clock skew (some cameras reject
        requests whose ``Created`` timestamp drifts too far from their own). The
        devicemgmt service is recreated afterwards so it is rebuilt with the
        freshly computed ``dt_diff``. A no-op when ``adjust_time`` is disabled.

        Called via ``_devicemgmt_with_time()`` so the clock-skew handshake
        happens regardless of whether ``update_xaddrs()`` or
        ``get_capabilities()`` runs first. Returns the devicemgmt service the
        caller should continue to use.
        """
        if not self.adjust_time:
            return devicemgmt
        try:
            sys_date = await devicemgmt.authless_GetSystemDateAndTime()
        except zeep.exceptions.Fault:
            # Looks like we should try with auth
            sys_date = await devicemgmt.GetSystemDateAndTime()
        cdate = sys_date.UTCDateTime
        cam_date = dt.datetime(
            cdate.Date.Year,
            cdate.Date.Month,
            cdate.Date.Day,
            cdate.Time.Hour,
            cdate.Time.Minute,
            cdate.Time.Second,
            tzinfo=dt.timezone.utc,
        )
        self.dt_diff = cam_date - dt.datetime.now(dt.timezone.utc)
        await devicemgmt.close()
        del self.services[devicemgmt.binding_key]
        return await self.create_devicemgmt_service()

    async def _update_xaddrs_from_services(self, devicemgmt: ONVIFService) -> bool:
        """Populate XAddrs from GetServices.

        GetServices is the ONVIF-recommended discovery method and returns every
        service the device exposes -- far more complete than GetCapabilities.
        Older devices may not implement it; a failure or empty response here is
        non-fatal and signals the caller to fall back to GetCapabilities.

        Returns True if at least one XAddr was discovered, False otherwise.
        """
        try:
            services = await devicemgmt.GetServices({"IncludeCapability": False})
        except (ONVIFError, zeep.exceptions.Fault) as err:
            # An unsupported GetServices raises zeep.exceptions.Fault directly
            # when awaited -- safe_func only wraps the synchronous request build,
            # not the await -- while other failures surface as ONVIFError (which
            # ONVIFTimeoutError subclasses). Either case is non-fatal: log the
            # underlying error so unexpected failures stay visible, and let the
            # caller fall back to GetCapabilities.
            logger.debug(
                "%s: Could not get services via GetServices: %s", self.host, err
            )
            return False
        found = False
        for service in services or []:
            try:
                namespace = service.Namespace
                xaddr = service.XAddr
            except AttributeError:
                # Skipping malformed entries is expected, handled behaviour, so
                # log at debug with host and entry detail rather than emitting a
                # full traceback per bad entry (noisy in production).
                logger.debug(
                    "%s: Skipping malformed service entry from GetServices: %r",
                    self.host,
                    service,
                )
                continue
            if namespace and xaddr:
                self.xaddrs[namespace] = self.rewrite_url(normalize_url(xaddr))
                found = True
        return found

    async def _update_xaddrs_from_capabilities(self, devicemgmt: ONVIFService) -> None:
        """Populate XAddrs and capabilities from GetCapabilities.

        GetCapabilities only advertises the top-level services
        (Analytics/Device/Events/Imaging/Media/PTZ); services nested under the
        Extension element (recording, replay, search, ...) are not discoverable
        here -- those come from GetServices. This is the fallback for devices
        that do not implement GetServices and is also the only source for
        self._capabilities, which GetServices does not return.
        """
        capabilities = await devicemgmt.GetCapabilities({"Category": "All"})
        for name in capabilities:
            capability = capabilities[name]
            try:
                if name.lower() in SERVICES and capability is not None:
                    namespace = SERVICES[name.lower()]["ns"]
                    self.xaddrs[namespace] = self.rewrite_url(
                        normalize_url(capability["XAddr"])
                    )
            except (KeyError, TypeError, AttributeError) as err:
                # Narrow to the parse-error shapes a malformed capability
                # entry can produce (missing XAddr, non-string key, non-dict
                # capability). Any other exception is a genuine bug and must
                # propagate rather than hide behind a log line.
                logger.debug(
                    "%s: Skipping malformed capability %s: %s",
                    self.host,
                    name,
                    err,
                )
        try:
            self._capabilities = self.to_dict(capabilities)
        except ONVIFError as err:
            # to_dict is @safe_func, so any serialization failure surfaces as
            # ONVIFError; catch that specifically so unrelated bugs propagate.
            logger.debug("%s: Failed to parse capabilities: %s", self.host, err)

    def has_broken_relative_time(
        self,
        expected_interval: dt.timedelta,
        current_time: dt.datetime | None,
        termination_time: dt.datetime | None,
    ) -> bool:
        """Mark timestamps as broken if a subscribe request returns an unexpected result."""
        logger.debug(
            "%s: Checking for broken relative timestamps: expected_interval: %s, current_time: %s, termination_time: %s",
            self.host,
            expected_interval,
            current_time,
            termination_time,
        )
        if not current_time:
            logger.debug("%s: Device returned no current time", self.host)
            return False
        if not termination_time:
            logger.debug("%s: Device returned no current time", self.host)
            return False
        if current_time.tzinfo is None:
            logger.debug(
                "%s: Device returned no timezone info for current time", self.host
            )
            return False
        if termination_time.tzinfo is None:
            logger.debug(
                "%s: Device returned no timezone info for termination time", self.host
            )
            return False
        actual_interval = termination_time - current_time
        if abs(actual_interval.total_seconds()) < (
            expected_interval.total_seconds() / 2
        ):
            logger.debug(
                "%s: Broken relative timestamps detected, switching to absolute timestamps: expected interval: %s, actual interval: %s",
                self.host,
                expected_interval,
                actual_interval,
            )
            self._has_broken_relative_timestamps = True
            return True
        logger.debug(
            "%s: Relative timestamps OK: expected interval: %s, actual interval: %s",
            self.host,
            expected_interval,
            actual_interval,
        )
        return False

    def get_next_termination_time(self, duration: dt.timedelta) -> str:
        """Calculate subscription absolute termination time."""
        if not self._has_broken_relative_timestamps:
            return f"PT{int(duration.total_seconds())}S"
        absolute_time: dt.datetime = utcnow() + duration
        if dt_diff := self.dt_diff:
            absolute_time += dt_diff
        return absolute_time.isoformat(timespec="seconds").replace("+00:00", "Z")

    async def create_pullpoint_manager(
        self,
        interval: dt.timedelta,
        subscription_lost_callback: Callable[[], None],
    ) -> PullPointManager:
        """Create a pullpoint manager."""
        manager = PullPointManager(self, interval, subscription_lost_callback)
        await manager.start()
        return manager

    async def create_notification_manager(
        self,
        address: str,
        interval: dt.timedelta,
        subscription_lost_callback: Callable[[], None],
    ) -> NotificationManager:
        """Create a notification manager."""
        manager = NotificationManager(
            self, address, interval, subscription_lost_callback
        )
        await manager.start()
        return manager

    async def close(self) -> None:
        """Close all transports."""
        await self._snapshot_client.close()
        await self._snapshot_connector.close()
        for service in self.services.values():
            await service.close()

    async def get_snapshot_uri(self, profile_token: str) -> str:
        """Get the snapshot uri for a given profile."""
        uri = self._snapshot_uris.get(profile_token, _SENTINEL)
        if uri is _SENTINEL:
            media_service = await self.create_media_service()
            req = media_service.create_type("GetSnapshotUri")
            req.ProfileToken = profile_token
            uri = None
            try:
                result = await media_service.GetSnapshotUri(req)
            except zeep.exceptions.Fault as error:
                logger.warning(
                    "%s: Failed to get snapshot URI for profile %s: %s",
                    self.host,
                    profile_token,
                    error,
                )
            else:
                try:
                    uri = self.rewrite_url(normalize_url(result.Uri))
                except (AttributeError, KeyError):
                    # AttributeError is raised when result.Uri is missing
                    # https://github.com/home-assistant/core/issues/135494
                    logger.warning(
                        "%s: The device returned an invalid snapshot URI", self.host
                    )
            self._snapshot_uris[profile_token] = uri
        return uri

    async def get_snapshot(
        self, profile_token: str, basic_auth: bool = False
    ) -> bytes | None:
        """Get a snapshot image from the camera."""
        uri = await self.get_snapshot_uri(profile_token)
        if uri is None:
            return None

        auth: BasicAuth | None = None
        middlewares: tuple[DigestAuthMiddleware, ...] | None = None

        if self.user and self.passwd:
            if basic_auth:
                auth = BasicAuth(self.user, self.passwd)
            else:
                # Use DigestAuthMiddleware for digest auth
                middlewares = (DigestAuthMiddleware(self.user, self.passwd),)

        response = await self._try_snapshot_uri(uri, auth=auth, middlewares=middlewares)
        content = await self._try_read_snapshot_content(uri, response)

        # If the request fails with a 401, strip user/pass from URL and retry
        if (
            response.status == 401
            and (stripped_uri := strip_user_pass_url(uri))
            and stripped_uri != uri
        ):
            response = await self._try_snapshot_uri(
                stripped_uri, auth=auth, middlewares=middlewares
            )
            content = await self._try_read_snapshot_content(uri, response)

        if response.status == 401:
            msg = f"Failed to authenticate to {uri}"
            raise ONVIFAuthError(msg)

        if response.status < 300:
            return content

        return None

    @handle_snapshot_errors
    async def _try_read_snapshot_content(
        self,
        uri: str,
        response: aiohttp.ClientResponse,
    ) -> bytes:
        """Try to read the snapshot URI."""
        return await response.read()

    @handle_snapshot_errors
    async def _try_snapshot_uri(
        self,
        uri: str,
        auth: BasicAuth | None = None,
        middlewares: tuple[DigestAuthMiddleware, ...] | None = None,
    ) -> aiohttp.ClientResponse:
        return await self._snapshot_client.get(uri, auth=auth, middlewares=middlewares)

    def get_definition(
        self, name: str, port_type: str | None = None
    ) -> tuple[str, str, str]:
        """Returns xaddr and wsdl of specified service"""
        # Check if the service is supported
        if name not in SERVICES:
            msg = f"Unknown service {name}"
            raise ONVIFError(msg)
        wsdl_file = SERVICES[name]["wsdl"]
        namespace = SERVICES[name]["ns"]

        binding_name = "{{{}}}{}".format(namespace, SERVICES[name]["binding"])

        if port_type:
            namespace += "/" + port_type

        wsdlpath = str(Path(self.wsdl_dir) / wsdl_file)
        cached_files = _WSDL_DIR_FILES.get(self.wsdl_dir)
        exists = (
            wsdl_file in cached_files
            if cached_files is not None
            else path_isfile(wsdlpath)
        )
        if not exists:
            msg = f"No such file: {wsdlpath}"
            raise ONVIFError(msg)

        # XAddr for devicemgmt is fixed:
        if name == "devicemgmt":
            xaddr = "{}:{}/onvif/device_service".format(
                self.host
                if (self.host.startswith("http://") or self.host.startswith("https://"))
                else f"http://{self.host}",
                self.port,
            )
            return xaddr, wsdlpath, binding_name

        # Get other XAddr
        xaddr = self.xaddrs.get(namespace)
        if not xaddr:
            msg = f"Device doesn`t support service: {name} with namespace {namespace}"
            raise ONVIFError(msg)

        return xaddr, wsdlpath, binding_name

    async def create_onvif_service(
        self,
        name: str,
        port_type: str | None = None,
        read_timeout: int | None = None,
        write_timeout: int | None = None,
    ) -> ONVIFService:
        """Create ONVIF service client"""
        name = name.lower()
        # Don't re-create bindings if the xaddr remains the same.
        # The xaddr can change when a new PullPointSubscription is created.
        binding_key = (name, port_type)

        # The first call for a given wsdl_dir does a single os.listdir off the
        # event loop and caches the result, so get_definition and
        # ONVIFService.__init__ can answer "does this wsdl exist" without
        # blocking I/O.
        if self.wsdl_dir not in _WSDL_DIR_FILES:
            _WSDL_DIR_FILES[self.wsdl_dir] = await asyncio.to_thread(
                _list_wsdl_dir, self.wsdl_dir
            )

        xaddr, wsdl_file, binding_name = self.get_definition(name, port_type)

        existing_service = self.services.get(binding_key)
        if existing_service:
            if existing_service.xaddr == xaddr:
                return existing_service
            # Close the existing service since it's no longer valid.
            # This can happen when a new PullPointSubscription is created.
            logger.debug(
                "Closing service %s with %s", binding_key, existing_service.xaddr
            )
            # Hold a reference to the task so it doesn't get
            # garbage collected before it completes.
            await existing_service.close()
            self.services.pop(binding_key)

        logger.debug("Creating service %s with %s", binding_key, xaddr)

        service = ONVIFService(
            xaddr,
            self.user,
            self.passwd,
            wsdl_file,
            self.encrypt,
            no_cache=self.no_cache,
            dt_diff=self.dt_diff,
            binding_name=binding_name,
            binding_key=binding_key,
            read_timeout=read_timeout,
            write_timeout=write_timeout,
        )
        await service.setup()

        self.services[binding_key] = service

        return service

    async def create_devicemgmt_service(self) -> ONVIFService:
        """Service creation helper."""
        return await self.create_onvif_service("devicemgmt")

    async def create_media_service(self) -> ONVIFService:
        """Service creation helper."""
        return await self.create_onvif_service("media")

    async def create_ptz_service(self) -> ONVIFService:
        """Service creation helper."""
        return await self.create_onvif_service("ptz")

    async def create_imaging_service(self) -> ONVIFService:
        """Service creation helper."""
        return await self.create_onvif_service("imaging")

    async def create_deviceio_service(self) -> ONVIFService:
        """Service creation helper."""
        return await self.create_onvif_service("deviceio")

    async def create_events_service(self) -> ONVIFService:
        """Service creation helper."""
        return await self.create_onvif_service("events")

    async def create_analytics_service(self) -> ONVIFService:
        """Service creation helper."""
        return await self.create_onvif_service("analytics")

    async def create_recording_service(self) -> ONVIFService:
        """Service creation helper."""
        return await self.create_onvif_service("recording")

    async def create_search_service(self) -> ONVIFService:
        """Service creation helper."""
        return await self.create_onvif_service("search")

    async def create_replay_service(self) -> ONVIFService:
        """Service creation helper."""
        return await self.create_onvif_service("replay")

    async def create_pullpoint_service(self) -> ONVIFService:
        """Service creation helper."""
        return await self.create_onvif_service(
            "pullpoint",
            port_type="PullPointSubscription",
            read_timeout=_PULLPOINT_TIMEOUT,
            write_timeout=_PULLPOINT_TIMEOUT,
        )

    async def create_notification_service(self) -> ONVIFService:
        """Service creation helper."""
        return await self.create_onvif_service("notification")

    async def create_subscription_service(
        self, port_type: str | None = None
    ) -> ONVIFService:
        """Service creation helper."""
        return await self.create_onvif_service("subscription", port_type=port_type)

    async def create_receiver_service(self) -> ONVIFService:
        """Service creation helper."""
        return await self.create_onvif_service("receiver")
