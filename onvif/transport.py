"""ONVIF transport."""
from __future__ import annotations

import os.path

from zeep.transports import Transport

from .util import path_isfile


class AsyncSafeTransport(Transport):
    """A transport that blocks all remote I/O for zeep."""

    def load(self, url: str) -> None:
        """Load the given XML document."""
        if not path_isfile(url):
            raise RuntimeError(f"Loading {url} is not supported in async mode")
        with open(os.path.expanduser(url), "rb") as fh:
            return fh.read()


ASYNC_TRANSPORT = AsyncSafeTransport()
