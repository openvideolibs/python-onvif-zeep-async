"""Tests for WS-Security UsernameToken authentication."""

from __future__ import annotations

import base64
import hashlib

from lxml import etree

from onvif.client import UsernameDigestTokenDtDiff

WSSE_NS = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
)
WSU_NS = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
)
DIGEST_TYPE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
)
TEXT_TYPE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-username-token-profile-1.0#PasswordText"
)


def _empty_envelope() -> etree._Element:
    nsmap = {"s": "http://www.w3.org/2003/05/soap-envelope"}
    envelope = etree.Element(
        "{http://www.w3.org/2003/05/soap-envelope}Envelope", nsmap=nsmap
    )
    etree.SubElement(envelope, "{http://www.w3.org/2003/05/soap-envelope}Header")
    etree.SubElement(envelope, "{http://www.w3.org/2003/05/soap-envelope}Body")
    return envelope


def _apply(user: str, passwd: str, *, use_digest: bool) -> etree._Element:
    token = UsernameDigestTokenDtDiff(user, passwd, use_digest=use_digest)
    envelope, _ = token.apply(_empty_envelope(), {})
    return envelope


def test_digest_created_uses_zulu_timestamp() -> None:
    """The Created timestamp must be canonical UTC 'Zulu' form, not +00:00.

    ONVIF cameras (notably Hikvision) reject the numeric "+00:00" offset that
    zeep emits by default, which makes digest authentication fail. See
    https://github.com/openvideolibs/python-onvif-zeep-async/issues/122
    """
    envelope = _apply("admin", "pass1234", use_digest=True)
    created = envelope.find(f".//{{{WSU_NS}}}Created")
    assert created is not None
    assert created.text is not None
    assert created.text.endswith("Z")
    assert "+00:00" not in created.text


def test_digest_password_type_is_digest() -> None:
    """With encrypt/use_digest enabled the password is a PasswordDigest."""
    envelope = _apply("admin", "pass1234", use_digest=True)
    password = envelope.find(f".//{{{WSSE_NS}}}Password")
    assert password is not None
    assert password.get("Type") == DIGEST_TYPE
    # The cleartext password must never appear in a digest token.
    assert password.text != "pass1234"


def test_digest_value_consistent_with_zulu_created() -> None:
    """The digest must hash the same Zulu timestamp string that is sent.

    Per WS-Security: digest = Base64(SHA1(nonce + created + password)). The
    server recomputes this from the Created element it receives, so the digest
    and the Created element must agree on the timestamp format.
    """
    password = "pass1234"
    envelope = _apply("admin", password, use_digest=True)

    nonce_el = envelope.find(f".//{{{WSSE_NS}}}Nonce")
    created_el = envelope.find(f".//{{{WSU_NS}}}Created")
    password_el = envelope.find(f".//{{{WSSE_NS}}}Password")

    assert nonce_el is not None
    assert nonce_el.text is not None
    assert created_el is not None
    assert created_el.text is not None
    assert password_el is not None
    assert password_el.text is not None

    nonce = base64.b64decode(nonce_el.text)
    expected = base64.b64encode(
        hashlib.sha1(  # noqa: S324 - WS-Security mandates SHA-1
            nonce + created_el.text.encode("utf-8") + password.encode("utf-8")
        ).digest()
    ).decode("ascii")

    assert password_el.text == expected


def test_plaintext_password_when_digest_disabled() -> None:
    """With encrypt/use_digest disabled the password is sent as PasswordText."""
    envelope = _apply("admin", "pass1234", use_digest=False)
    password = envelope.find(f".//{{{WSSE_NS}}}Password")
    assert password is not None
    assert password.get("Type") == TEXT_TYPE
    assert password.text == "pass1234"
