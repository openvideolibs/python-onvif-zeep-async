"""ONVIF Client."""
import datetime as dt
import logging
import os.path

import httpx
from httpx import AsyncClient, BasicAuth, DigestAuth
from zeep.cache import SqliteCache
from zeep.client import AsyncClient as BaseZeepAsyncClient, Client, Settings
from zeep.exceptions import Fault
import zeep.helpers
from zeep.proxy import AsyncServiceProxy
from zeep.transports import AsyncTransport
from zeep.wsse.username import UsernameToken

from onvif.definition import SERVICES
from onvif.exceptions import ONVIFAuthError, ONVIFError, ONVIFTimeoutError

logger = logging.getLogger("onvif")
logging.basicConfig(level=logging.INFO)
logging.getLogger("zeep.client").setLevel(logging.CRITICAL)


def safe_func(func):
    """Ensure methods to raise an ONVIFError Exception when some thing was wrong."""

    def wrapped(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as err:
            raise ONVIFError(err)

    return wrapped


class UsernameDigestTokenDtDiff(UsernameToken):
    """
    UsernameDigestToken class, with a time offset parameter that can be adjusted;
    This allows authentication on cameras without being time synchronized.
    Please note that using NTP on both end is the recommended solution,
    this should only be used in "safe" environments.
    """

    def __init__(self, user, passw, dt_diff=None, **kwargs):
        super().__init__(user, passw, **kwargs)
        # Date/time difference in datetime.timedelta
        self.dt_diff = dt_diff

    def apply(self, envelope, headers):
        old_created = self.created
        if self.created is None:
            self.created = dt.datetime.utcnow()
        if self.dt_diff is not None:
            self.created += self.dt_diff
        result = super().apply(envelope, headers)
        self.created = old_created
        return result


class ZeepAsyncClient(BaseZeepAsyncClient):
    """Overwrite create_service method to be async."""

    def create_service(self, binding_name, address):
        """Create a new ServiceProxy for the given binding name and address.
        :param binding_name: The QName of the binding
        :param address: The address of the endpoint
        """
        try:
            binding = self.wsdl.bindings[binding_name]
        except KeyError:
            raise ValueError(
                "No binding found with the given QName. Available bindings "
                "are: %s" % (", ".join(self.wsdl.bindings.keys()))
            )
        return AsyncServiceProxy(self, binding, address=address)


class ONVIFService:
    """
    Python Implemention for ONVIF Service.
    Services List:
        DeviceMgmt DeviceIO Event AnalyticsDevice Display Imaging Media
        PTZ Receiver RemoteDiscovery Recording Replay Search Extension

    >>> from onvif import ONVIFService
    >>> device_service = ONVIFService('http://192.168.0.112/onvif/device_service',
    ...                           'admin', 'foscam',
    ...                           '/etc/onvif/wsdl/devicemgmt.wsdl')
    >>> ret = device_service.GetHostname()
    >>> print ret.FromDHCP
    >>> print ret.Name
    >>> device_service.SetHostname(dict(Name='newhostname'))
    >>> ret = device_service.GetSystemDateAndTime()
    >>> print ret.DaylightSavings
    >>> print ret.TimeZone
    >>> dict_ret = device_service.to_dict(ret)
    >>> print dict_ret['TimeZone']

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
        xaddr,
        user,
        passwd,
        url,
        encrypt=True,
        no_cache=False,
        dt_diff=None,
        binding_name="",
    ):
        if not os.path.isfile(url):
            raise ONVIFError("%s doesn`t exist!" % url)

        self.url = url
        self.xaddr = xaddr
        wsse = UsernameDigestTokenDtDiff(
            user, passwd, dt_diff=dt_diff, use_digest=encrypt
        )
        # Create soap client
        client = AsyncClient(timeout=90)
        self.transport = (
            AsyncTransport(client=client)
            if no_cache
            else AsyncTransport(client=client, cache=SqliteCache())
        )
        settings = Settings()
        settings.strict = False
        settings.xml_huge_tree = True
        self.zeep_client = ZeepAsyncClient(
            wsdl=url, wsse=wsse, transport=self.transport, settings=settings
        )
        self.ws_client = self.zeep_client.create_service(binding_name, self.xaddr)

        # Set soap header for authentication
        self.user = user
        self.passwd = passwd
        # Indicate wether password digest is needed
        self.encrypt = encrypt
        self.dt_diff = dt_diff

        namespace = binding_name[binding_name.find("{") + 1 : binding_name.find("}")]
        available_ns = self.zeep_client.namespaces
        active_ns = (
            list(available_ns.keys())[list(available_ns.values()).index(namespace)]
            or "ns0"
        )
        self.create_type = lambda x: self.zeep_client.get_element(active_ns + ":" + x)()

    async def close(self):
        """Close the transport."""
        await self.transport.aclose()

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

        def service_wrapper(func):
            """Wrap service call."""

            @safe_func
            def wrapped(params=None):
                def call(params=None):
                    # No params
                    if params is None:
                        params = {}
                    else:
                        params = ONVIFService.to_dict(params)
                    try:
                        ret = func(**params)
                    except TypeError:
                        ret = func(params)
                    return ret

                return call(params)

            return wrapped

        builtin = name.startswith("__") and name.endswith("__")
        if builtin:
            return self.__dict__[name]
        return service_wrapper(getattr(self.ws_client, name))


class ONVIFCamera:
    """
    Python Implemention ONVIF compliant device
    This class integrates onvif services

    adjust_time parameter allows authentication on cameras without being time synchronized.
    Please note that using NTP on both end is the recommended solution,
    this should only be used in "safe" environments.
    Also, this cannot be used on AXIS camera, as every request is authenticated, contrary to ONVIF standard

    >>> from onvif import ONVIFCamera
    >>> mycam = ONVIFCamera('192.168.0.112', 80, 'admin', '12345')
    >>> mycam.devicemgmt.GetServices(False)
    >>> media_service = mycam.create_media_service()
    >>> ptz_service = mycam.create_ptz_service()
    # Get PTZ Configuration:
    >>> mycam.ptz.GetConfiguration()
    # Another way:
    >>> ptz_service.GetConfiguration()
    """

    def __init__(
        self,
        host,
        port,
        user,
        passwd,
        wsdl_dir=os.path.join(os.path.dirname(os.path.dirname(__file__)), "wsdl"),
        encrypt=True,
        no_cache=False,
        adjust_time=False,
    ):
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
        self.dt_diff = None
        self.xaddrs = {}

        # Active service client container
        self.services = {}

        self.to_dict = ONVIFService.to_dict

        self._snapshot_uris = {}
        self._snapshot_client = AsyncClient()

    async def update_xaddrs(self):
        """Update xaddrs for services."""
        self.dt_diff = None
        devicemgmt = self.create_devicemgmt_service()
        if self.adjust_time:
            sys_date = await devicemgmt.GetSystemDateAndTime()
            cdate = sys_date.UTCDateTime
            cam_date = dt.datetime(
                cdate.Date.Year,
                cdate.Date.Month,
                cdate.Date.Day,
                cdate.Time.Hour,
                cdate.Time.Minute,
                cdate.Time.Second,
            )
            self.dt_diff = cam_date - dt.datetime.utcnow()

        # Get XAddr of services on the device
        self.xaddrs = {}
        capabilities = await devicemgmt.GetCapabilities({"Category": "All"})
        for name in capabilities:
            capability = capabilities[name]
            try:
                if name.lower() in SERVICES and capability is not None:
                    namespace = SERVICES[name.lower()]["ns"]
                    self.xaddrs[namespace] = capability["XAddr"]
            except Exception:
                logger.exception("Unexpected service type")

    async def create_pullpoint_subscription(self):
        """Create a pullpoint subscription."""
        try:
            events = self.create_events_service()
            pullpoint = await events.CreatePullPointSubscription()
            # pylint: disable=protected-access
            self.xaddrs[
                "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription"
            ] = pullpoint.SubscriptionReference.Address._value_1
        except Fault:
            return False
        return True

    async def close(self):
        """Close all transports."""
        await self._snapshot_client.aclose()
        for service in self.services.values():
            await service.close()

    async def get_snapshot_uri(self, profile_token):
        """Get the snapshot uri for a given profile."""
        uri = self._snapshot_uris.get(profile_token)
        if uri is None:
            media_service = self.create_media_service()
            req = media_service.create_type("GetSnapshotUri")
            req.ProfileToken = profile_token
            result = await media_service.GetSnapshotUri(req)
            uri = result.Uri
            self._snapshot_uris[profile_token] = uri
        return uri

    async def get_snapshot(self, profile_token, basic_auth=False):
        """Get a snapshot image from the camera."""
        uri = await self.get_snapshot_uri(profile_token)
        if uri is None:
            return None

        auth = None
        if self.user and self.passwd:
            if basic_auth:
                auth = BasicAuth(self.user, self.passwd)
            else:
                auth = DigestAuth(self.user, self.passwd)

        try:
            response = await self._snapshot_client.get(uri, auth=auth)
        except httpx.TimeoutException as error:
            raise ONVIFTimeoutError(error) from error
        except httpx.RequestError as error:
            raise ONVIFError(error) from error

        if response.status_code == 401:
            raise ONVIFAuthError(f"Failed to authenticate to {uri}")

        if response.status_code < 300:
            return response.content

        return None

    def get_definition(self, name, port_type=None):
        """Returns xaddr and wsdl of specified service"""
        # Check if the service is supported
        if name not in SERVICES:
            raise ONVIFError("Unknown service %s" % name)
        wsdl_file = SERVICES[name]["wsdl"]
        namespace = SERVICES[name]["ns"]

        binding_name = "{{{}}}{}".format(namespace, SERVICES[name]["binding"])

        if port_type:
            namespace += "/" + port_type

        wsdlpath = os.path.join(self.wsdl_dir, wsdl_file)
        if not os.path.isfile(wsdlpath):
            raise ONVIFError("No such file: %s" % wsdlpath)

        # XAddr for devicemgmt is fixed:
        if name == "devicemgmt":
            xaddr = "{}:{}/onvif/device_service".format(
                self.host
                if (self.host.startswith("http://") or self.host.startswith("https://"))
                else "http://%s" % self.host,
                self.port,
            )
            return xaddr, wsdlpath, binding_name

        # Get other XAddr
        xaddr = self.xaddrs.get(namespace)
        if not xaddr:
            raise ONVIFError("Device doesn`t support service: %s" % name)

        return xaddr, wsdlpath, binding_name

    def create_onvif_service(self, name, port_type=None):
        """Create ONVIF service client"""

        name = name.lower()
        xaddr, wsdl_file, binding_name = self.get_definition(name, port_type)

        # Don't re-create bindings if the xaddr remains the same.
        # The xaddr can change when a new PullPointSubscription is created.
        binding_key = f"{binding_name}{xaddr}"
        binding = self.services.get(binding_key)
        if binding:
            return binding

        service = ONVIFService(
            xaddr,
            self.user,
            self.passwd,
            wsdl_file,
            self.encrypt,
            no_cache=self.no_cache,
            dt_diff=self.dt_diff,
            binding_name=binding_name,
        )

        self.services[binding_key] = service

        return service

    def create_devicemgmt_service(self):
        """Service creation helper."""
        return self.create_onvif_service("devicemgmt")

    def create_media_service(self):
        """Service creation helper."""
        return self.create_onvif_service("media")

    def create_ptz_service(self):
        """Service creation helper."""
        return self.create_onvif_service("ptz")

    def create_imaging_service(self):
        """Service creation helper."""
        return self.create_onvif_service("imaging")

    def create_deviceio_service(self):
        """Service creation helper."""
        return self.create_onvif_service("deviceio")

    def create_events_service(self):
        """Service creation helper."""
        return self.create_onvif_service("events")

    def create_analytics_service(self):
        """Service creation helper."""
        return self.create_onvif_service("analytics")

    def create_recording_service(self):
        """Service creation helper."""
        return self.create_onvif_service("recording")

    def create_search_service(self):
        """Service creation helper."""
        return self.create_onvif_service("search")

    def create_replay_service(self):
        """Service creation helper."""
        return self.create_onvif_service("replay")

    def create_pullpoint_service(self):
        """Service creation helper."""
        return self.create_onvif_service("pullpoint", port_type="PullPointSubscription")

    def create_notification_service(self):
        """Service creation helper."""
        return self.create_onvif_service("notification")

    def create_subscription_service(self, port_type=None):
        """Service creation helper."""
        return self.create_onvif_service("subscription", port_type=port_type)

    def create_receiver_service(self):
        """Service creation helper."""
        return self.create_onvif_service("receiver")
