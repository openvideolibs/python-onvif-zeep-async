import uuid
from typing import ClassVar

from lxml import etree
from lxml.builder import ElementMaker
from zeep import ns
from zeep.plugins import Plugin
from zeep.wsdl.utils import get_or_create_header

WSA = ElementMaker(namespace=ns.WSA, nsmap={"wsa": ns.WSA})


class WsAddressingIfMissingPlugin(Plugin):
    nsmap: ClassVar[dict[str, str]] = {"wsa": ns.WSA}

    def __init__(self, address_url: str | None = None):
        self.address_url = address_url

    def egress(self, envelope, http_headers, operation, binding_options):
        """Apply the ws-addressing headers to the given envelope."""
        # Only the events/notification/pullpoint WSDLs declare wsam/wsaw:Action;
        # for those operations zeep populates ``operation.abstract.wsa_action``.
        # Other ONVIF services (PTZ, media, devicemgmt, imaging, ...) do not
        # declare a WSA action and do not need WSA headers -- emitting them
        # anyway breaks some cameras (e.g. Meari PTZ Stop, issue #155) and is
        # not what other ONVIF clients (ODM, VMS, ...) send for these
        # operations.
        wsa_action = operation.abstract.wsa_action
        if not wsa_action:
            return envelope, http_headers

        header = get_or_create_header(envelope)
        for elem in header:
            if (elem.prefix or "").startswith("wsa"):
                # WSA header already exists
                return envelope, http_headers

        headers = [
            WSA.Action(wsa_action),
            WSA.MessageID("urn:uuid:" + str(uuid.uuid4())),
            WSA.To(self.address_url or binding_options["address"]),
        ]
        header.extend(headers)

        # the top_nsmap kwarg was added in lxml 3.5.0
        if etree.LXML_VERSION[:2] >= (3, 5):
            etree.cleanup_namespaces(
                header, keep_ns_prefixes=header.nsmap, top_nsmap=self.nsmap
            )
        else:
            etree.cleanup_namespaces(header)
        return envelope, http_headers
