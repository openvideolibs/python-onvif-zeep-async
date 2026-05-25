"""A fake Hikvision ONVIF camera for end-to-end tests.

This module spins up a real :mod:`aiohttp` web server that mimics how a
Hikvision IP camera answers ONVIF SOAP requests. It lets the test suite drive a
genuine :class:`onvif.ONVIFCamera` through the full request/response pipeline
(WSDL loading, WS-Security signing, zeep parsing) without any real hardware.

The canned responses are modelled on the quirks that real Hikvision firmware
exhibits and that this library has had to work around, e.g.:

* ``GetSystemDateAndTime`` reported with the structured date/time elements.
* Digest authentication that only succeeds when the ``Created`` timestamp uses
  the canonical UTC "Zulu" form (see issues #122 and #179).

Every received request is recorded on the server instance so tests can assert
on the bytes the client actually sent (operation name, WS-Security header, ...).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aiohttp import web
from lxml import etree

SOAP_ENV_NS = "http://www.w3.org/2003/05/soap-envelope"
WSSE_NS = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
)
WSU_NS = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
)

# Device service paths Hikvision cameras expose. Only the device service path is
# fixed by the client; the others are advertised through GetCapabilities.
DEVICE_SERVICE_PATH = "/onvif/device_service"
MEDIA_SERVICE_PATH = "/onvif/Media"
EVENTS_SERVICE_PATH = "/onvif/Events"
IMAGING_SERVICE_PATH = "/onvif/Imaging"
PTZ_SERVICE_PATH = "/onvif/PTZ"
ANALYTICS_SERVICE_PATH = "/onvif/Analytics"


def _soap_envelope(body: str) -> str:
    """Wrap a SOAP body fragment in a SOAP 1.2 response envelope."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<env:Envelope xmlns:env="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
        'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
        'xmlns:tev="http://www.onvif.org/ver10/events/wsdl" '
        'xmlns:wsa="http://www.w3.org/2005/08/addressing" '
        'xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2" '
        'xmlns:tt="http://www.onvif.org/ver10/schema">'
        f"<env:Body>{body}</env:Body>"
        "</env:Envelope>"
    )


def _soap_fault(reason: str, subcode: str = "ter:NotAuthorized") -> str:
    """Build a SOAP 1.2 fault envelope mimicking an ONVIF error."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<env:Envelope xmlns:env="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:ter="http://www.onvif.org/ver10/error">'
        "<env:Body><env:Fault>"
        "<env:Code><env:Value>env:Sender</env:Value>"
        f"<env:Subcode><env:Value>{subcode}</env:Value></env:Subcode></env:Code>"
        f'<env:Reason><env:Text xml:lang="en">{reason}</env:Text></env:Reason>'
        "</env:Fault></env:Body></env:Envelope>"
    )


@dataclass
class RecordedRequest:
    """A single SOAP request received by the fake camera."""

    operation: str
    body: bytes
    path: str

    @property
    def envelope(self) -> etree._Element:
        """Parse the request body into an lxml element tree."""
        return etree.fromstring(self.body)

    @property
    def created(self) -> str | None:
        """Return the WS-Security ``Created`` timestamp text, if present."""
        el = self.envelope.find(f".//{{{WSU_NS}}}Created")
        return el.text if el is not None else None

    @property
    def password_type(self) -> str | None:
        """Return the WS-Security password ``Type`` attribute, if present."""
        el = self.envelope.find(f".//{{{WSSE_NS}}}Password")
        return el.get("Type") if el is not None else None

    @property
    def username(self) -> str | None:
        """Return the WS-Security ``Username`` text, if present."""
        el = self.envelope.find(f".//{{{WSSE_NS}}}Username")
        return el.text if el is not None else None


@dataclass
class FakeHikvisionCamera:
    """An aiohttp app that answers ONVIF SOAP requests like a Hikvision camera.

    The ``utc_*`` fields control the value returned by ``GetSystemDateAndTime`` so
    a test can simulate a camera whose clock drifts from the host. ``require_auth``,
    when set, makes the camera reject requests that lack a WS-Security
    ``UsernameToken`` with a 401-style SOAP fault.
    """

    utc_year: int = 2024
    utc_month: int = 8
    utc_day: int = 17
    utc_hour: int = 12
    utc_minute: int = 30
    utc_second: int = 45
    require_auth: bool = False
    # PullPoint subscription timestamps. Hikvision firmware famously emits an
    # xs:dateTime using "-" as the date/time separator (e.g. 2024-08-17-12:30:45Z)
    # which only parses thanks to FastDateTime (see issue #178). The defaults
    # advertise a 60-second subscription window using that exact format.
    pullpoint_current_time: str = "2024-08-17-12:30:45Z"
    pullpoint_termination_time: str = "2024-08-17-12:31:45Z"

    requests: list[RecordedRequest] = field(default_factory=list)
    base_url: str = ""
    _runner: web.AppRunner | None = None
    _site: web.TCPSite | None = None

    # -- response builders -------------------------------------------------

    def _capabilities_response(self) -> str:
        base = self.base_url
        return _soap_envelope(
            "<tds:GetCapabilitiesResponse><tds:Capabilities>"
            "<tt:Analytics>"
            f"<tt:XAddr>{base}{ANALYTICS_SERVICE_PATH}</tt:XAddr>"
            "<tt:RuleSupport>true</tt:RuleSupport>"
            "<tt:AnalyticsModuleSupport>true</tt:AnalyticsModuleSupport>"
            "</tt:Analytics>"
            "<tt:Device>"
            f"<tt:XAddr>{base}{DEVICE_SERVICE_PATH}</tt:XAddr>"
            "</tt:Device>"
            "<tt:Events>"
            f"<tt:XAddr>{base}{EVENTS_SERVICE_PATH}</tt:XAddr>"
            "<tt:WSSubscriptionPolicySupport>true</tt:WSSubscriptionPolicySupport>"
            "<tt:WSPullPointSupport>true</tt:WSPullPointSupport>"
            "<tt:WSPausableSubscriptionManagerInterfaceSupport>false"
            "</tt:WSPausableSubscriptionManagerInterfaceSupport>"
            "</tt:Events>"
            "<tt:Imaging>"
            f"<tt:XAddr>{base}{IMAGING_SERVICE_PATH}</tt:XAddr>"
            "</tt:Imaging>"
            "<tt:Media>"
            f"<tt:XAddr>{base}{MEDIA_SERVICE_PATH}</tt:XAddr>"
            "<tt:StreamingCapabilities>"
            "<tt:RTPMulticast>false</tt:RTPMulticast>"
            "<tt:RTP_TCP>true</tt:RTP_TCP>"
            "<tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP>"
            "</tt:StreamingCapabilities>"
            "</tt:Media>"
            "<tt:PTZ>"
            f"<tt:XAddr>{base}{PTZ_SERVICE_PATH}</tt:XAddr>"
            "</tt:PTZ>"
            "</tds:Capabilities></tds:GetCapabilitiesResponse>"
        )

    def _device_information_response(self) -> str:
        return _soap_envelope(
            "<tds:GetDeviceInformationResponse>"
            "<tds:Manufacturer>HIKVISION</tds:Manufacturer>"
            "<tds:Model>DS-2CD2085FWD-I</tds:Model>"
            "<tds:FirmwareVersion>V5.5.82 build 190909</tds:FirmwareVersion>"
            "<tds:SerialNumber>DS-2CD2085FWD-I20190101AAWR000000000</tds:SerialNumber>"
            "<tds:HardwareId>88</tds:HardwareId>"
            "</tds:GetDeviceInformationResponse>"
        )

    def _system_date_and_time_response(self) -> str:
        return _soap_envelope(
            "<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>"
            "<tt:DateTimeType>NTP</tt:DateTimeType>"
            "<tt:DaylightSavings>false</tt:DaylightSavings>"
            "<tt:TimeZone><tt:TZ>CST-8:00:00</tt:TZ></tt:TimeZone>"
            "<tt:UTCDateTime>"
            "<tt:Time>"
            f"<tt:Hour>{self.utc_hour}</tt:Hour>"
            f"<tt:Minute>{self.utc_minute}</tt:Minute>"
            f"<tt:Second>{self.utc_second}</tt:Second>"
            "</tt:Time>"
            "<tt:Date>"
            f"<tt:Year>{self.utc_year}</tt:Year>"
            f"<tt:Month>{self.utc_month}</tt:Month>"
            f"<tt:Day>{self.utc_day}</tt:Day>"
            "</tt:Date>"
            "</tt:UTCDateTime>"
            "</tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>"
        )

    def _snapshot_uri_response(self) -> str:
        return _soap_envelope(
            "<trt:GetSnapshotUriResponse><trt:MediaUri>"
            f"<tt:Uri>{self.base_url}/onvif-http/snapshot?Profile_1</tt:Uri>"
            "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
            "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>"
            "<tt:Timeout>PT0S</tt:Timeout>"
            "</trt:MediaUri></trt:GetSnapshotUriResponse>"
        )

    def _create_pullpoint_subscription_response(self) -> str:
        return _soap_envelope(
            "<tev:CreatePullPointSubscriptionResponse>"
            "<tev:SubscriptionReference>"
            f"<wsa:Address>{self.base_url}/onvif/Subscription?Idx=1</wsa:Address>"
            "</tev:SubscriptionReference>"
            f"<wsnt:CurrentTime>{self.pullpoint_current_time}</wsnt:CurrentTime>"
            f"<wsnt:TerminationTime>{self.pullpoint_termination_time}</wsnt:TerminationTime>"
            "</tev:CreatePullPointSubscriptionResponse>"
        )

    def _response_for(self, operation: str) -> str | None:
        builders = {
            "GetCapabilities": self._capabilities_response,
            "GetDeviceInformation": self._device_information_response,
            "GetSystemDateAndTime": self._system_date_and_time_response,
            "GetSnapshotUri": self._snapshot_uri_response,
            "CreatePullPointSubscription": (
                self._create_pullpoint_subscription_response
            ),
        }
        builder = builders.get(operation)
        return builder() if builder else None

    # -- request handling --------------------------------------------------

    @staticmethod
    def _operation_name(body: bytes) -> str:
        """Extract the SOAP operation (first body child's local name)."""
        envelope = etree.fromstring(body)
        soap_body = envelope.find(f"{{{SOAP_ENV_NS}}}Body")
        if soap_body is None or len(soap_body) == 0:
            return ""
        return etree.QName(soap_body[0]).localname

    @staticmethod
    def _has_auth(body: bytes) -> bool:
        envelope = etree.fromstring(body)
        return envelope.find(f".//{{{WSSE_NS}}}UsernameToken") is not None

    async def _handle(self, request: web.Request) -> web.Response:
        body = await request.read()
        operation = self._operation_name(body)
        self.requests.append(
            RecordedRequest(operation=operation, body=body, path=request.path)
        )

        if self.require_auth and not self._has_auth(body):
            return web.Response(
                status=401,
                body=_soap_fault("Authentication required").encode(),
                content_type="application/soap+xml",
            )

        response = self._response_for(operation)
        if response is None:
            return web.Response(
                status=500,
                body=_soap_fault(
                    f"Unknown operation {operation}", subcode="ter:ActionNotSupported"
                ).encode(),
                content_type="application/soap+xml",
            )

        return web.Response(
            body=response.encode(),
            content_type="application/soap+xml",
            charset="utf-8",
        )

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> str:
        """Start the server on an ephemeral port and return its base URL."""
        app = web.Application()
        for path in (
            DEVICE_SERVICE_PATH,
            MEDIA_SERVICE_PATH,
            EVENTS_SERVICE_PATH,
            IMAGING_SERVICE_PATH,
            PTZ_SERVICE_PATH,
            ANALYTICS_SERVICE_PATH,
        ):
            app.router.add_post(path, self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()
        # Resolve the ephemeral port that aiohttp bound to.
        server = self._site._server
        port = server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        return self.base_url

    async def stop(self) -> None:
        """Shut the server down."""
        if self._site is not None:
            await self._site.stop()
        if self._runner is not None:
            await self._runner.cleanup()

    @property
    def host(self) -> str:
        """The host the camera bound to (without scheme/port)."""
        return "127.0.0.1"

    @property
    def port(self) -> int:
        """The ephemeral port the camera bound to."""
        return int(self.base_url.rsplit(":", 1)[1])

    def operations(self) -> list[str]:
        """Return the ordered list of operation names received so far."""
        return [req.operation for req in self.requests]

    def last_request(self, operation: str) -> RecordedRequest | None:
        """Return the most recent recorded request for ``operation``."""
        for req in reversed(self.requests):
            if req.operation == operation:
                return req
        return None
