"""AIOHTTP transport for zeep."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from zeep.transports import Transport
from zeep.utils import get_version
from zeep.wsdl.utils import etree_to_string

import aiohttp
import httpx
from aiohttp import ClientResponse, ClientSession, ClientTimeout, TCPConnector
from requests import Response

if TYPE_CHECKING:
    from lxml.etree import _Element

_LOGGER = logging.getLogger(__name__)


class AIOHTTPTransport(Transport):
    """Async transport using aiohttp."""

    def __init__(
        self,
        session: ClientSession | None = None,
        timeout: float = 300,
        operation_timeout: float | None = None,
        verify_ssl: bool = True,
        proxy: str | None = None,
    ) -> None:
        """
        Initialize the transport.

        Args:
            session: The aiohttp ClientSession to use
            timeout: The default timeout for requests in seconds
            operation_timeout: The default timeout for operations in seconds
            verify_ssl: Whether to verify SSL certificates
            proxy: Proxy URL to use

        """
        super().__init__(
            cache=None,
            timeout=timeout,
            operation_timeout=operation_timeout,
        )

        # Override parent's session with aiohttp session
        self.session = session
        self.timeout = timeout
        self.operation_timeout = operation_timeout
        self.verify_ssl = verify_ssl
        self.proxy = proxy
        self._close_session = session is None

    async def __aenter__(self) -> AIOHTTPTransport:
        """Enter async context."""
        if self.session is None:
            connector = TCPConnector(ssl=self.verify_ssl)
            timeout = ClientTimeout(total=self.timeout)
            self.session = ClientSession(connector=connector, timeout=timeout)
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit async context."""
        if self._close_session and self.session:
            await self.session.close()
            self.session = None

    async def aclose(self) -> None:
        """Close the transport session."""
        if self.session:
            await self.session.close()

    def _aiohttp_to_httpx_response(
        self, aiohttp_response: ClientResponse, content: bytes
    ) -> httpx.Response:
        """Convert aiohttp ClientResponse to httpx Response."""
        # Create httpx Response with the content
        httpx_response = httpx.Response(
            status_code=aiohttp_response.status,
            headers=httpx.Headers(aiohttp_response.headers),
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
            for cookie in aiohttp_response.cookies.values():
                httpx_response.cookies.set(
                    cookie.key,
                    cookie.value,
                    domain=cookie.get("domain"),
                    path=cookie.get("path"),
                )

        return httpx_response

    def _aiohttp_to_requests_response(
        self, aiohttp_response: ClientResponse, content: bytes
    ) -> Response:
        """Convert aiohttp ClientResponse directly to requests Response."""
        new = Response()
        new._content = content
        new.status_code = aiohttp_response.status
        new.headers = dict(aiohttp_response.headers)
        new.cookies = aiohttp_response.cookies
        new.encoding = aiohttp_response.charset
        return new

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
        if self.session is None:
            async with self:
                return await self._post(address, message, headers)
        return await self._post(address, message, headers)

    async def _post(
        self, address: str, message: str, headers: dict[str, str]
    ) -> httpx.Response:
        """Internal POST implementation."""
        _LOGGER.debug("HTTP Post to %s:\n%s", address, message)

        # Set default headers
        headers = headers or {}
        headers.setdefault("User-Agent", f"Zeep/{get_version()}")
        headers.setdefault("Content-Type", 'text/xml; charset="utf-8"')

        # Determine timeout
        timeout = self.operation_timeout or self.timeout
        if timeout:
            client_timeout = ClientTimeout(total=timeout)
        else:
            client_timeout = None

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
                timeout=client_timeout,
            )
            response.raise_for_status()

            # Read the content to log it
            content = await response.read()
            _LOGGER.debug(
                "HTTP Response from %s (status: %d):\n%s",
                address,
                response.status,
                content.decode("utf-8", errors="replace"),
            )

            # Convert to httpx Response
            return self._aiohttp_to_httpx_response(response, content)

        except TimeoutError as exc:
            raise TimeoutError(f"Request to {address} timed out") from exc
        except aiohttp.ClientError as exc:
            raise ConnectionError(f"Error connecting to {address}: {exc}") from exc

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
        response = await self.post(address, message, headers)
        return self._httpx_to_requests_response(response)

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
        if self.session is None:
            async with self:
                return await self._get(address, params, headers)
        return await self._get(address, params, headers)

    async def _get(
        self,
        address: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        """Internal GET implementation."""
        _LOGGER.debug("HTTP Get from %s", address)

        # Set default headers
        headers = headers or {}
        headers.setdefault("User-Agent", f"Zeep/{get_version()}")

        # Determine timeout
        timeout = self.operation_timeout or self.timeout
        if timeout:
            client_timeout = ClientTimeout(total=timeout)
        else:
            client_timeout = None

        try:
            response = await self.session.get(
                address,
                params=params,
                headers=headers,
                proxy=self.proxy,
                timeout=client_timeout,
            )
            response.raise_for_status()

            # Read content
            content = await response.read()

            _LOGGER.debug(
                "HTTP Response from %s (status: %d)",
                address,
                response.status,
            )

            # Convert directly to requests.Response
            return self._aiohttp_to_requests_response(response, content)

        except TimeoutError as exc:
            raise TimeoutError(f"Request to {address} timed out") from exc
        except aiohttp.ClientError as exc:
            raise ConnectionError(f"Error connecting to {address}: {exc}") from exc

    def _httpx_to_requests_response(self, response: httpx.Response) -> Response:
        """Convert an httpx.Response object to a requests.Response object"""
        body = response.read()

        new = Response()
        new._content = body
        new.status_code = response.status_code
        new.headers = response.headers
        new.cookies = response.cookies
        new.encoding = response.encoding
        return new

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
