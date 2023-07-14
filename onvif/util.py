"""ONVIF util."""
from __future__ import annotations

import contextlib
import datetime as dt
from functools import lru_cache, partial
import os
import ssl
from typing import Any
from urllib.parse import ParseResultBytes, urlparse, urlunparse

from zeep.exceptions import Fault

utcnow: partial[dt.datetime] = partial(dt.datetime.now, dt.timezone.utc)

# This does blocking I/O (stat) so we cache the result
# to minimize the impact of the blocking I/O.
path_isfile = lru_cache(maxsize=128)(os.path.isfile)


def normalize_url(url: bytes | str | None) -> str | None:
    """Normalize URL.

    Some cameras respond with <wsa5:Address>http://192.168.1.106:8106:8106/onvif/Subscription?Idx=43</wsa5:Address>
    https://github.com/home-assistant/core/issues/92603#issuecomment-1537213126
    """
    if url is None:
        return None
    parsed = urlparse(url)
    # If the URL is not a string, return None
    if isinstance(parsed, ParseResultBytes):
        return None
    if "[" not in parsed.netloc and parsed.netloc.count(":") > 1:
        net_location = parsed.netloc.split(":", 3)
        net_location.pop()
        return urlunparse(parsed._replace(netloc=":".join(net_location)))
    return url


def extract_subcodes_as_strings(subcodes: Any) -> list[str]:
    """Stringify ONVIF subcodes."""
    if isinstance(subcodes, list):
        return [code.text if hasattr(code, "text") else str(code) for code in subcodes]
    return [str(subcodes)]


def stringify_onvif_error(error: Exception) -> str:
    """Stringify ONVIF error."""
    if isinstance(error, Fault):
        message = error.message
        if error.detail is not None:  # checking true is deprecated
            # Detail may be a bytes object, so we need to convert it to string
            if isinstance(error.detail, bytes):
                detail = error.detail.decode("utf-8", "replace")
            else:
                detail = str(error.detail)
            message += ": " + detail
        if error.code is not None:  # checking true is deprecated
            message += f" (code:{error.code})"
        if error.subcodes is not None:  # checking true is deprecated
            message += (
                f" (subcodes:{','.join(extract_subcodes_as_strings(error.subcodes))})"
            )
        if error.actor:
            message += f" (actor:{error.actor})"
    else:
        message = str(error)
    return message or f"Device sent empty error with type {type(error)}"


def is_auth_error(error: Exception) -> bool:
    """Return True if error is an authentication error.

    Most of the tested cameras do not return a proper error code when
    authentication fails, so we need to check the error message as well.
    """
    if not isinstance(error, Fault):
        return False
    return (
        any(
            "NotAuthorized" in code
            for code in extract_subcodes_as_strings(error.subcodes)
        )
        or "auth" in stringify_onvif_error(error).lower()
    )


def create_no_verify_ssl_context() -> ssl.SSLContext:
    """Return an SSL context that does not verify the server certificate.
    This is a copy of aiohttp's create_default_context() function, with the
    ssl verify turned off and old SSL versions enabled.

    https://github.com/aio-libs/aiohttp/blob/33953f110e97eecc707e1402daa8d543f38a189b/aiohttp/connector.py#L911
    """
    sslcontext = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    sslcontext.check_hostname = False
    sslcontext.verify_mode = ssl.CERT_NONE
    # Allow all ciphers rather than only Python 3.10 default
    sslcontext.set_ciphers("DEFAULT")
    with contextlib.suppress(AttributeError):
        # This only works for OpenSSL >= 1.0.0
        sslcontext.options |= ssl.OP_NO_COMPRESSION
    sslcontext.set_default_verify_paths()
    # ssl.OP_LEGACY_SERVER_CONNECT is only available in Python 3.12a4+
    sslcontext.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
    return sslcontext
