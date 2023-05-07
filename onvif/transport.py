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
        # Ideally this would happen in the executor but the library
        # does not call this from a coroutine so the best we can do
        # without a major refactor is to cache this so it only happens
        # once per process at startup. Previously it would happen once
        # per service per camera per setup which is a lot of blocking
        # I/O in the event loop so this is a major improvement.
        with open(os.path.expanduser(url), "rb") as fh:
            return fh.read()


ASYNC_TRANSPORT = AsyncSafeTransport()
