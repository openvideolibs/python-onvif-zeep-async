"""Ingress plugin that repairs malformed SOAP 1.2 fault subcodes.

SOAP 1.2 requires every ``env:Subcode`` element to contain an ``env:Value``
child. Some cameras send an empty ``Subcode`` instead, e.g. the TP-Link NC450
answering ``GetServices``::

    <SOAP-ENV:Code>
      <SOAP-ENV:Value>SOAP-ENV:Sender</SOAP-ENV:Value>
      <SOAP-ENV:Subcode></SOAP-ENV:Subcode>
    </SOAP-ENV:Code>

zeep assumes the ``Value`` child is present and raises ``AttributeError``
while building the fault, instead of ``zeep.exceptions.Fault``. Callers that
handle unsupported operations by catching ``Fault`` -- such as the
``GetServices`` -> ``GetCapabilities`` fallback in ``update_xaddrs`` -- never
get the chance, and device setup fails.

Dropping the empty ``Subcode`` before zeep parses the fault turns the reply
into a regular ``Fault``. Well-formed subcodes are left untouched.
"""

from __future__ import annotations

from zeep import ns
from zeep.plugins import Plugin

_SUBCODE = f"{{{ns.SOAP_ENV_12}}}Subcode"
_VALUE = f"{{{ns.SOAP_ENV_12}}}Value"


class StripEmptyFaultSubcodePlugin(Plugin):
    """Remove SOAP 1.2 ``Subcode`` elements that lack a ``Value`` child."""

    def ingress(self, envelope, http_headers, operation):
        """Drop empty subcodes so zeep raises ``Fault`` instead of crashing."""
        for subcode in [
            el for el in envelope.iter(_SUBCODE) if el.find(_VALUE) is None
        ]:
            parent = subcode.getparent()
            if parent is not None:
                parent.remove(subcode)
        return envelope, http_headers
