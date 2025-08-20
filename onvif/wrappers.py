"""ONVIF Client wrappers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

import aiohttp

from .const import BACKOFF_TIME, DEFAULT_ATTEMPTS

P = ParamSpec("P")
T = TypeVar("T")
logger = logging.getLogger("onvif")


def retry_connection_error(
    attempts: int = DEFAULT_ATTEMPTS,
    exception: type[Exception] = aiohttp.ClientError,
    backoff: float | None = None,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """Define a wrapper to retry on connection error."""
    if backoff is None:
        backoff = BACKOFF_TIME

    def _decorator_retry_connection_error(
        func: Callable[P, Awaitable[T]],
    ) -> Callable[P, Awaitable[T]]:
        """
        Define a wrapper to retry on connection error.

        The remote server is allowed to disconnect us any time so
        we need to retry the operation.
        """

        async def _async_wrap_connection_error_retry(  # type: ignore[return]
            *args: P.args, **kwargs: P.kwargs
        ) -> T:
            logger.debug(
                "retry_connection_error wrapper called for %s with exception=%s, attempts=%s",
                func.__name__,
                exception,
                attempts,
            )
            for attempt in range(attempts):
                try:
                    logger.debug(
                        "Attempt %s/%s for %s", attempt + 1, attempts, func.__name__
                    )
                    return await func(*args, **kwargs)
                except exception as ex:
                    #
                    # We should only need to retry on ServerDisconnectedError but some cameras
                    # are flakey and sometimes do not respond to the Renew request so we
                    # retry on ClientError as well.
                    #
                    # For ServerDisconnectedError:
                    # http://datatracker.ietf.org/doc/html/rfc2616#section-8.1.4 allows the server
                    # to close the connection at any time, we treat this as a normal and try again
                    # once since we do not want to declare the camera as not supporting PullPoint
                    # if it just happened to close the connection at the wrong time.
                    logger.debug(
                        "Caught exception %s (type: %s) on attempt %s/%s",
                        ex,
                        type(ex).__name__,
                        attempt + 1,
                        attempts,
                        exc_info=True,
                    )
                    if attempt == attempts - 1:
                        logger.debug("Final attempt failed, re-raising exception")
                        raise
                    logger.debug(
                        "Error: %s while calling %s, backing off: %s, retrying...",
                        ex,
                        func,
                        backoff,
                        exc_info=True,
                    )
                    await asyncio.sleep(backoff)

        return _async_wrap_connection_error_retry

    return _decorator_retry_connection_error
