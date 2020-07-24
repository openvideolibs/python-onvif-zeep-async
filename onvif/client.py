"""ONVIF Client."""

import datetime as dt
import os.path
import logging

from aiohttp import ClientSession
from zeep.asyncio import AsyncTransport
from zeep.cache import SqliteCache
from zeep.client import Client, CachingClient, Settings
from zeep.exceptions import Fault
from zeep.wsse.username import UsernameToken
import zeep.helpers

from onvif.exceptions import ONVIFError
from onvif.definition import SERVICES

logger = logging.getLogger("onvif")
logging.basicConfig(level=logging.INFO)
logging.getLogger("zeep.client").setLevel(logging.CRITICAL)


def safe_func(func):
    """Ensure methods to raise an ONVIFError Exception when some thing was wrong."""

    def wrapped(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as err:
            # print('Ouuups: err =', err, ', func =', func, ', args =', args, ', kwargs =', kwargs)
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
        zeep_client=None,
        no_cache=False,
        dt_diff=None,
        binding_name="",
        transport=None,
    ):
        if not os.path.isfile(url):
            raise ONVIFError("%s doesn`t exist!" % url)

        self.url = url
        self.xaddr = xaddr
        self.transport = transport
        wsse = UsernameDigestTokenDtDiff(
            user, passwd, dt_diff=dt_diff, use_digest=encrypt
        )
        # Create soap client
        if not zeep_client:
            if not self.transport:
                session = ClientSession()
                self.transport = (
                    AsyncTransport(None, session=session)
                    if no_cache
                    else AsyncTransport(None, session=session, cache=SqliteCache())
                )
            ClientType = Client if no_cache else CachingClient
            settings = Settings()
            settings.strict = False
            settings.xml_huge_tree = True
            self.zeep_client = ClientType(
                wsdl=url, wsse=wsse, transport=self.transport, settings=settings
            )
        else:
            self.zeep_client = zeep_client
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
        """Close the transport session."""
        await self.transport.session.close()

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
        transport=None,
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
        self.transport = transport
        self.dt_diff = None
        self.xaddrs = {}

        # Active service client container
        self.services = {}

        self.to_dict = ONVIFService.to_dict

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
        for service in self.services.values():
            await service.close()

    def get_definition(self, name, port_type=None):
        """Returns xaddr and wsdl of specified service"""
        # Check if the service is supported
        if name not in SERVICES:
            raise ONVIFError("Unknown service %s" % name)
        wsdl_file = SERVICES[name]["wsdl"]
        namespace = SERVICES[name]["ns"]

        binding_name = "{%s}%s" % (namespace, SERVICES[name]["binding"])

        if port_type:
            namespace += "/" + port_type

        wsdlpath = os.path.join(self.wsdl_dir, wsdl_file)
        if not os.path.isfile(wsdlpath):
            raise ONVIFError("No such file: %s" % wsdlpath)

        # XAddr for devicemgmt is fixed:
        if name == "devicemgmt":
            xaddr = "%s:%s/onvif/device_service" % (
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
        binding = self.services.get(binding_name)
        if binding and binding.xaddr == xaddr:
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
            transport=self.transport,
        )

        self.services[binding_name] = service

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
