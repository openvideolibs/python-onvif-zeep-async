"""AIOHTTP transport for zeep."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from zeep.cache import SqliteCache
from zeep.transports import Transport
from zeep.utils import get_version
from zeep.wsdl.utils import etree_to_string
from multidict import CIMultiDict
import httpx
from aiohttp import ClientResponse, ClientSession, hdrs
from requests import Response
from requests.structures import CaseInsensitiveDict

if TYPE_CHECKING:
    from lxml.etree import _Element

_LOGGER = logging.getLogger(__name__)


class AIOHTTPTransport(Transport):
    """Async transport using aiohttp."""

    def __init__(
        self,
        session: ClientSession,
        verify_ssl: bool = True,
        proxy: str | None = None,
        cache: SqliteCache | None = None,
    ) -> None:
        """
        Initialize the transport.

        Args:
            session: The aiohttp ClientSession to use (required). The session's
                     timeout configuration will be used for all requests.
            verify_ssl: Whether to verify SSL certificates
            proxy: Proxy URL to use

        """
        super().__init__(
            cache=cache,
            timeout=session.timeout.total,
            operation_timeout=session.timeout.sock_read,
        )

        # Override parent's session with aiohttp session
        self.session = session
        self.verify_ssl = verify_ssl
        self.proxy = proxy
        self._close_session = False  # Never close a provided session
        # Extract timeout from session
        self._client_timeout = session.timeout

    async def __aenter__(self) -> AIOHTTPTransport:
        """Enter async context."""
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit async context."""

    async def aclose(self) -> None:
        """Close the transport session."""

    def _filter_headers(self, headers: CIMultiDict[str]) -> list[tuple[str, str]]:
        """Filter out Content-Encoding header.

        Since aiohttp has already decompressed the content, we need to
        remove the Content-Encoding header to prevent zeep from trying
        to decompress it again, which would cause a zlib error.
        """
        return [(k, v) for k, v in headers.items() if k != hdrs.CONTENT_ENCODING]

    def _aiohttp_to_httpx_response(
        self, aiohttp_response: ClientResponse, content: bytes
    ) -> httpx.Response:
        """Convert aiohttp ClientResponse to httpx Response."""
        # Create httpx Response with the content
        httpx_response = httpx.Response(
            status_code=aiohttp_response.status,
            headers=httpx.Headers(self._filter_headers(aiohttp_response.headers)),
            content=content,
            request=httpx.Request(
                method=aiohttp_response.method,
                url=str(aiohttp_response.url),
            ),
        )

        # Add encoding if available
        if aiohttp_response.charset:
            httpx_response._encoding = aiohttp_response.charset

        # Store cookies if any
        if aiohttp_response.cookies:
            for name, cookie in aiohttp_response.cookies.items():
                # httpx.Cookies.set only accepts name, value, domain, and path
                httpx_response.cookies.set(
                    name,
                    cookie.value,
                    domain=cookie.get("domain") or "",
                    path=cookie.get("path") or "/",
                )

        return httpx_response

    def _aiohttp_to_requests_response(
        self, aiohttp_response: ClientResponse, content: bytes
    ) -> Response:
        """Convert aiohttp ClientResponse directly to requests Response."""
        new = Response()
        new._content = content
        new.status_code = aiohttp_response.status
        # Use dict comprehension for requests.Response headers
        new.headers = CaseInsensitiveDict(
            self._filter_headers(aiohttp_response.headers)
        )
        # Convert aiohttp cookies to requests format
        if aiohttp_response.cookies:
            for name, cookie in aiohttp_response.cookies.items():
                new.cookies.set(
                    name,
                    cookie.value,
                    domain=cookie.get("domain"),
                    path=cookie.get("path"),
                )
        new.encoding = aiohttp_response.charset
        return new

    async def _post_internal(
        self, address: str, message: str | bytes, headers: dict[str, str]
    ) -> tuple[ClientResponse, bytes]:
        """Internal POST implementation that returns aiohttp response and content."""
        _LOGGER.debug("HTTP Post to %s:\n%s", address, message)

        # Set default headers
        headers = headers or {}
        headers.setdefault("User-Agent", f"Zeep/{get_version()}")
        headers.setdefault("Content-Type", 'text/xml; charset="utf-8"')

        # Handle both str and bytes
        if isinstance(message, str):
            data = message.encode("utf-8")
        else:
            data = message

        try:
            response = await self.session.post(
                address,
                data=data,
                headers=headers,
                proxy=self.proxy,
                timeout=self._client_timeout,
            )

            # Read the content to log it before checking status
            content = await response.read()
            _LOGGER.debug(
                "HTTP Response from %s (status: %d):\n%s",
                address,
                response.status,
                content,
            )

            return response, content
        except RuntimeError as exc:
            # Handle RuntimeError which may occur if the session is closed
            raise RuntimeError(f"Failed to post to {address}: {exc}") from exc
        except TimeoutError as exc:
            raise TimeoutError(f"Request to {address} timed out") from exc

    async def post(
        self, address: str, message: str, headers: dict[str, str]
    ) -> httpx.Response:
        """
        Perform async POST request.

        Args:
            address: The URL to send the request to
            message: The message to send
            headers: HTTP headers to include

        Returns:
            The httpx response object

        """
        response, content = await self._post_internal(address, message, headers)
        return self._aiohttp_to_httpx_response(response, content)

    async def post_xml(
        self, address: str, envelope: _Element, headers: dict[str, str]
    ) -> Response:
        """
        Post XML envelope and return parsed response.

        Args:
            address: The URL to send the request to
            envelope: The XML envelope to send
            headers: HTTP headers to include

        Returns:
            A Response object compatible with zeep

        """
        message = etree_to_string(envelope)
        response, content = await self._post_internal(address, message, headers)
        return self._aiohttp_to_requests_response(response, content)

    async def get(
        self,
        address: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        """
        Perform async GET request.

        Args:
            address: The URL to send the request to
            params: Query parameters
            headers: HTTP headers to include

        Returns:
            A Response object compatible with zeep

        """
        _LOGGER.debug("HTTP Get from %s", address)

        # Set default headers
        headers = headers or {}
        headers.setdefault("User-Agent", f"Zeep/{get_version()}")

        try:
            response = await self.session.get(
                address,
                params=params,
                headers=headers,
                proxy=self.proxy,
                timeout=self._client_timeout,
            )

            # Read content and log before checking status
            content = await response.read()

            _LOGGER.debug(
                "HTTP Response from %s (status: %d):\n%s",
                address,
                response.status,
                content,
            )

            # Convert directly to requests.Response
            return self._aiohttp_to_requests_response(response, content)
        except RuntimeError as exc:
            # Handle RuntimeError which may occur if the session is closed
            raise RuntimeError(f"Failed to get from {address}: {exc}") from exc

        except TimeoutError as exc:
            raise TimeoutError(f"Request to {address} timed out") from exc

    def load(self, url: str) -> bytes:
        """
        Load content from URL synchronously.

        This method runs the async get method in a new event loop.

        Args:
            url: The URL to load

        Returns:
            The content as bytes

        """
        # Create a new event loop for sync operation
        loop = asyncio.new_event_loop()
        try:
            response = loop.run_until_complete(self.get(url))
            return response.content
        finally:
            loop.close()
