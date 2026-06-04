"""ONVIF transport."""

from __future__ import annotations

from pathlib import Path

from zeep.transports import Transport

from .util import path_isfile


class AsyncSafeTransport(Transport):
    """A transport that blocks all remote I/O for zeep."""

    def load(self, url: str) -> bytes:
        """Load the given XML document."""
        if not path_isfile(url):
            msg = f"Loading {url} is not supported in async mode"
            raise RuntimeError(msg)
        with Path(url).expanduser().open("rb") as fh:
            return fh.read()


ASYNC_TRANSPORT = AsyncSafeTransport()
