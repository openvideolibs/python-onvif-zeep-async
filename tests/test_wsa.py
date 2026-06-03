"""Tests for the WS-Addressing egress plugin.

Verifies that WSA Action/MessageID/To headers are only added when the WSDL
operation declares ``wsa_action`` (events / pullpoint / notification). For
operations that do not declare it (PTZ, media, devicemgmt, ...), other ONVIF
software does not emit WSA headers, and some cameras (e.g. Meari) reject the
PTZ Stop request when extra WSA headers are present. See issue #155.
"""

from __future__ import annotations

from types import SimpleNamespace

from lxml import etree
from zeep import ns

from onvif.wsa import WsAddressingIfMissingPlugin

_SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
_NSMAP = {"soap": _SOAP_NS}


def _make_envelope() -> etree._Element:
    """Build an empty SOAP envelope with a Body element."""
    envelope = etree.Element(etree.QName(_SOAP_NS, "Envelope"), nsmap=_NSMAP)
    etree.SubElement(envelope, etree.QName(_SOAP_NS, "Body"))
    return envelope


def _make_operation(wsa_action: str | None, soapaction: str = "") -> SimpleNamespace:
    """Build a minimal ``operation`` shim matching what zeep passes to egress."""
    return SimpleNamespace(
        abstract=SimpleNamespace(wsa_action=wsa_action),
        soapaction=soapaction,
    )


def _wsa_children(envelope: etree._Element) -> list[str]:
    """Return local-names of WSA children in the SOAP Header."""
    header = envelope.find(f"{{{_SOAP_NS}}}Header")
    if header is None:
        return []
    return [
        etree.QName(child).localname
        for child in header
        if etree.QName(child).namespace == ns.WSA
    ]


def test_adds_wsa_headers_when_wsa_action_declared() -> None:
    """Operations with a WSDL-declared wsa_action get full WSA headers."""
    plugin = WsAddressingIfMissingPlugin()
    envelope = _make_envelope()
    operation = _make_operation(
        wsa_action="http://www.onvif.org/ver10/events/wsdl/EventPortType/CreatePullPointSubscriptionRequest",
    )

    result_env, _ = plugin.egress(
        envelope,
        http_headers={},
        operation=operation,
        binding_options={"address": "http://192.0.2.10/onvif/Events"},
    )

    assert _wsa_children(result_env) == ["Action", "MessageID", "To"]


def test_skips_wsa_headers_when_wsa_action_missing() -> None:
    """Operations without WSDL-declared wsa_action (e.g. PTZ Stop) get no WSA headers.

    Issue #155: some cameras (Meari, ...) refuse the PTZ Stop command when WSA
    Action appears in the SOAP Header. ONVIF Device Manager and similar clients
    do not emit WSA headers for these operations.
    """
    plugin = WsAddressingIfMissingPlugin()
    envelope = _make_envelope()
    operation = _make_operation(
        wsa_action=None,
        soapaction="http://www.onvif.org/ver20/ptz/wsdl/Stop",
    )

    result_env, _ = plugin.egress(
        envelope,
        http_headers={},
        operation=operation,
        binding_options={"address": "http://192.0.2.10/onvif/PTZ"},
    )

    assert _wsa_children(result_env) == []


def test_adds_wsa_headers_for_wsn_soapaction_only_operation() -> None:
    """WS-BaseNotification ops (Subscribe/Renew/Unsubscribe/...) still emit WSA.

    These operations declare no abstract ``wsaw:Action`` -- only a
    ``soap:operation soapAction`` in the OASIS ``wsn`` namespace -- yet they
    require WSA headers. The soapAction fallback must keep working for them
    (issue #155 must not regress the subscription-manager flows).
    """
    plugin = WsAddressingIfMissingPlugin()
    envelope = _make_envelope()
    operation = _make_operation(
        wsa_action=None,
        soapaction="http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/RenewRequest",
    )

    result_env, _ = plugin.egress(
        envelope,
        http_headers={},
        operation=operation,
        binding_options={"address": "http://192.0.2.10/onvif/Subscription"},
    )

    assert _wsa_children(result_env) == ["Action", "MessageID", "To"]
    action = result_env.find(f"{{{_SOAP_NS}}}Header/{{{ns.WSA}}}Action")
    assert (
        action.text
        == "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/RenewRequest"
    )


def test_preserves_existing_wsa_headers_with_arbitrary_prefix() -> None:
    """Pre-existing WSA headers are detected by namespace, not by the ``wsa`` prefix.

    XML namespace prefixes are arbitrary; an upstream caller may have added WSA
    elements under a different prefix (e.g. ``<a:Action xmlns:a="...wsa...">``).
    The plugin must still recognise them and not emit duplicates.
    """
    plugin = WsAddressingIfMissingPlugin()
    envelope = _make_envelope()
    header = etree.SubElement(envelope, etree.QName(_SOAP_NS, "Header"))
    pre_existing = etree.SubElement(
        header, etree.QName(ns.WSA, "Action"), nsmap={"a": ns.WSA}
    )
    pre_existing.text = "preset"
    operation = _make_operation(wsa_action="http://example.org/Action")

    result_env, _ = plugin.egress(
        envelope,
        http_headers={},
        operation=operation,
        binding_options={"address": "http://192.0.2.10/onvif/Events"},
    )

    assert _wsa_children(result_env) == ["Action"]
    assert result_env.find(f"{{{_SOAP_NS}}}Header/{{{ns.WSA}}}Action").text == "preset"


def test_skips_when_wsa_action_empty_string() -> None:
    """An empty-string wsa_action must be treated as ``not declared``."""
    plugin = WsAddressingIfMissingPlugin()
    envelope = _make_envelope()
    operation = _make_operation(wsa_action="", soapaction="urn:soapaction")

    result_env, _ = plugin.egress(
        envelope,
        http_headers={},
        operation=operation,
        binding_options={"address": "http://192.0.2.10/onvif/PTZ"},
    )

    assert _wsa_children(result_env) == []
