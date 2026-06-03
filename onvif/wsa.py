import uuid
from typing import ClassVar

from lxml import etree
from lxml.builder import ElementMaker
from zeep import ns
from zeep.plugins import Plugin
from zeep.wsdl.utils import get_or_create_header

WSA = ElementMaker(namespace=ns.WSA, nsmap={"wsa": ns.WSA})

# WS-BaseNotification operations (Subscribe / Renew / Unsubscribe / Pause /
# ResumeSubscription / ...) do not carry an abstract ``wsaw:Action`` in the
# WSDL; they only declare a ``soap:operation soapAction`` in the OASIS ``wsn``
# namespace. They still require WSA headers, so we fall back to the soapAction
# for them. PTZ / media / devicemgmt / imaging operations declare neither, and
# must not get WSA headers (issue #155).
_WSN_NAMESPACE = "http://docs.oasis-open.org/wsn/"


class WsAddressingIfMissingPlugin(Plugin):
    nsmap: ClassVar[dict[str, str]] = {"wsa": ns.WSA}

    def __init__(self, address_url: str | None = None):
        self.address_url = address_url

    def egress(self, envelope, http_headers, operation, binding_options):
        """Apply the ws-addressing headers to the given envelope."""
        # The events / pullpoint operations declare ``wsam/wsaw:Action`` in the
        # WSDL, so zeep populates ``operation.abstract.wsa_action`` for them.
        # The WS-BaseNotification lifecycle operations (Subscribe / Renew /
        # Unsubscribe / Pause / ...) declare only a ``wsn`` soapAction but still
        # need WSA, so we fall back to the soapAction for those. Every other
        # ONVIF service (PTZ, media, devicemgmt, imaging, ...) declares neither
        # and must not get WSA headers -- emitting them anyway breaks some
        # cameras (e.g. Meari PTZ Stop, issue #155) and is not what other ONVIF
        # clients (ODM, VMS, ...) send for those operations.
        wsa_action = operation.abstract.wsa_action
        if not wsa_action:
            soapaction = operation.soapaction or ""
            if not soapaction.startswith(_WSN_NAMESPACE):
                return envelope, http_headers
            wsa_action = soapaction

        header = get_or_create_header(envelope)
        for elem in header:
            tag = elem.tag
            if isinstance(tag, str) and etree.QName(tag).namespace == ns.WSA:
                # WSA header already exists (regardless of prefix used)
                return envelope, http_headers

        headers = [
            WSA.Action(wsa_action),
            WSA.MessageID("urn:uuid:" + str(uuid.uuid4())),
            WSA.To(self.address_url or binding_options["address"]),
        ]
        header.extend(headers)

        etree.cleanup_namespaces(
            header, keep_ns_prefixes=header.nsmap, top_nsmap=self.nsmap
        )
        return envelope, http_headers
