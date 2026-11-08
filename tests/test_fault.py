"""Tests for the SOAP 1.2 fault subcode ingress plugin.

Some cameras (e.g. TP-Link NC450) send a SOAP 1.2 fault with an empty
``Subcode`` element. zeep then raises ``AttributeError`` instead of ``Fault``,
which defeats fallbacks that catch ``Fault``. The plugin strips such subcodes
before zeep parses the fault.
"""

from __future__ import annotations

import pytest
from lxml import etree
from zeep.exceptions import Fault
from zeep.wsdl.bindings.soap import Soap12Binding

from onvif.fault import StripEmptyFaultSubcodePlugin

_ENV = "http://www.w3.org/2003/05/soap-envelope"

# Verbatim GetServices reply body from a TP-Link NC450 (firmware 1.5.6 Build
# 200924), trimmed to the namespaces it uses.
_NC450_GET_SERVICES_FAULT = (
    '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope">'
    "<SOAP-ENV:Body><SOAP-ENV:Fault>"
    "<SOAP-ENV:Code><SOAP-ENV:Value>SOAP-ENV:Sender</SOAP-ENV:Value>"
    "<SOAP-ENV:Subcode></SOAP-ENV:Subcode></SOAP-ENV:Code>"
    '<SOAP-ENV:Reason><SOAP-ENV:Text xml:lang="en">'
    "Method 'GetServices' not implemented: method name or namespace not recognized"
    "</SOAP-ENV:Text></SOAP-ENV:Reason>"
    "</SOAP-ENV:Fault></SOAP-ENV:Body></SOAP-ENV:Envelope>"
)


def _fault(code_children: str) -> etree._Element:
    """Build a SOAP 1.2 fault envelope with the given ``env:Code`` children."""
    return etree.fromstring(
        f'<env:Envelope xmlns:env="{_ENV}" '
        'xmlns:ter="http://www.onvif.org/ver10/error">'
        "<env:Body><env:Fault>"
        f"<env:Code><env:Value>env:Sender</env:Value>{code_children}</env:Code>"
        '<env:Reason><env:Text xml:lang="en">msg</env:Text></env:Reason>'
        "</env:Fault></env:Body></env:Envelope>"
    )


def _ingress(envelope: etree._Element) -> etree._Element:
    result, headers = StripEmptyFaultSubcodePlugin().ingress(
        envelope, http_headers={"h": "v"}, operation=None
    )
    assert headers == {"h": "v"}
    return result


def _raise_fault(envelope: etree._Element) -> Fault:
    """Run zeep's SOAP 1.2 fault parser and return the raised Fault."""
    binding = Soap12Binding.__new__(Soap12Binding)
    with pytest.raises(Fault) as exc_info:
        binding.process_error(envelope, None)
    return exc_info.value


def _subcode_names(fault: Fault) -> list[str]:
    return [qname.localname for qname in fault.subcodes]


def test_nc450_get_services_fault_becomes_regular_fault() -> None:
    """The real NC450 reply is turned into a Fault with its reason intact."""
    envelope = _ingress(etree.fromstring(_NC450_GET_SERVICES_FAULT))
    fault = _raise_fault(envelope)
    assert "GetServices' not implemented" in fault.message
    assert fault.subcodes == []


@pytest.mark.parametrize(
    ("code_children", "expected"),
    [
        pytest.param("<env:Subcode></env:Subcode>", [], id="empty"),
        pytest.param("<env:Subcode/>", [], id="self-closing"),
        pytest.param(
            "<env:Subcode><env:Value>ter:NotAuthorized</env:Value></env:Subcode>",
            ["NotAuthorized"],
            id="valid-untouched",
        ),
        pytest.param(
            "<env:Subcode><env:Value>ter:ActionNotSupported</env:Value>"
            "<env:Subcode/></env:Subcode>",
            ["ActionNotSupported"],
            id="valid-with-empty-nested",
        ),
        pytest.param(
            "<env:Subcode><env:Value>ter:Receiver</env:Value>"
            "<env:Subcode><env:Value>ter:ActionNotSupported</env:Value>"
            "</env:Subcode></env:Subcode>",
            ["Receiver", "ActionNotSupported"],
            id="valid-nested-untouched",
        ),
        pytest.param("", [], id="no-subcode"),
    ],
)
def test_subcodes_after_ingress(code_children: str, expected: list[str]) -> None:
    """Empty subcodes are dropped; well-formed ones survive unchanged."""
    fault = _raise_fault(_ingress(_fault(code_children)))
    assert _subcode_names(fault) == expected


def test_non_fault_envelope_is_untouched() -> None:
    """Regular responses pass through byte-for-byte."""
    envelope = etree.fromstring(
        f'<env:Envelope xmlns:env="{_ENV}"><env:Body>'
        "<tds:GetServicesResponse "
        'xmlns:tds="http://www.onvif.org/ver10/device/wsdl"/>'
        "</env:Body></env:Envelope>"
    )
    before = etree.tostring(envelope)
    assert etree.tostring(_ingress(envelope)) == before
