"""ONVIF Client wrappers."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable
import logging
from typing import Callable, ParamSpec, TypeVar

import httpx

from .const import BACKOFF_TIME, DEFAULT_ATTEMPTS

P = ParamSpec("P")
T = TypeVar("T")
logger = logging.getLogger("onvif")


def retry_connection_error(
    attempts: int = DEFAULT_ATTEMPTS,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """Define a wrapper to retry on connection error."""

    def _decorator_retry_connection_error(
        func: Callable[P, Awaitable[T]]
    ) -> Callable[P, Awaitable[T]]:
        """Define a wrapper to retry on connection error.

        The remote server is allowed to disconnect us any time so
        we need to retry the operation.
        """

        async def _async_wrap_connection_error_retry(  # type: ignore[return]
            *args: P.args, **kwargs: P.kwargs
        ) -> T:
            for attempt in range(attempts):
                try:
                    return await func(*args, **kwargs)
                except httpx.RequestError as ex:
                    #
                    # We should only need to retry on RemoteProtocolError but some cameras
                    # are flakey and sometimes do not respond to the Renew request so we
                    # retry on RequestError as well.
                    #
                    # For RemoteProtocolError:
                    # http://datatracker.ietf.org/doc/html/rfc2616#section-8.1.4 allows the server
                    # to close the connection at any time, we treat this as a normal and try again
                    # once since we do not want to declare the camera as not supporting PullPoint
                    # if it just happened to close the connection at the wrong time.
                    if attempt == attempts - 1:
                        raise
                    logger.debug(
                        "Error: %s while calling %s, backing off: %s, retrying...",
                        ex,
                        func,
                        BACKOFF_TIME,
                        exc_info=True,
                    )
                    await asyncio.sleep(BACKOFF_TIME)

        return _async_wrap_connection_error_retry

    return _decorator_retry_connection_error
