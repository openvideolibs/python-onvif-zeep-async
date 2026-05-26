"""Test ServerDisconnectedError retry mechanism with a mock HTTP server."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import ClientSession, web
from lxml import etree

import onvif
from onvif.client import AsyncTransportProtocolErrorHandler, ONVIFService
from onvif.zeep_aiohttp import AIOHTTPTransport

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator


@pytest.fixture
def mock_etree_to_string() -> Generator[MagicMock]:
    """Mock etree_to_string to return test XML."""
    with patch("onvif.zeep_aiohttp.etree_to_string", return_value=b"<test/>") as mock:
        yield mock


class DisconnectingHTTPProtocol(asyncio.Protocol):
    """HTTP protocol that disconnects after each response without sending Connection: close."""

    def __init__(self, server: DisconnectingServer) -> None:
        self.server: DisconnectingServer = server
        self.transport: asyncio.Transport | None = None
        self.buffer: bytes = b""

    def connection_made(self, transport: asyncio.Transport) -> None:
        """Called when a connection is established."""
        self.transport = transport

    def data_received(self, data: bytes) -> None:
        """Handle incoming HTTP request."""
        self.buffer += data

        # Simple HTTP parsing - look for double CRLF indicating end of headers
        if b"\r\n\r\n" not in self.buffer:
            return  # Wait for more data

        headers_end = self.buffer.index(b"\r\n\r\n") + 4
        headers = self.buffer[:headers_end].decode("utf-8", errors="ignore")

        # Check if we have Content-Length header
        if b"Content-Length:" in self.buffer:
            # Extract content length
            for line in headers.split("\r\n"):
                if line.startswith("Content-Length:"):
                    content_length = int(line.split(":")[1].strip())

                    # Check if we have the full body
                    if len(self.buffer) >= headers_end + content_length:
                        self.process_request()
                    # else wait for more data
                    break
        else:
            # No body expected
            self.process_request()

    def process_request(self) -> None:
        """Process the HTTP request and send response."""
        self.server.request_count += 1

        # Create HTTP response without Connection: close header
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/soap+xml\r\n"
            b"Content-Length: %d\r\n"
            b"\r\n"
        )

        body = (
            b"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope">
    <SOAP-ENV:Body>
        <tds:TestResponse>
            <tds:RequestNumber>%d</tds:RequestNumber>
        </tds:TestResponse>
    </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
            % self.server.request_count
        )

        response = response % len(body) + body

        # Send response
        self.transport.write(response)

        # Clear buffer for next request
        self.buffer = b""

        # Abruptly close connection after sending response
        # This simulates the problematic camera behavior
        asyncio.get_event_loop().call_later(0.01, self.transport.close)

    def connection_lost(self, exc: Exception | None) -> None:
        """Called when the connection is lost."""


class DisconnectingServer:
    """Mock server that closes connection after each response without Connection: close header."""

    def __init__(self) -> None:
        self.request_count: int = 0
        self.server: asyncio.Server | None = None
        self.port: int | None = None

    async def start(self, port: int = 0) -> str:
        """Start the mock server."""
        loop = asyncio.get_event_loop()

        # Create server with custom protocol
        self.server = await loop.create_server(
            lambda: DisconnectingHTTPProtocol(self), "localhost", port
        )

        # Get the actual port if 0 was specified
        self.port = self.server.sockets[0].getsockname()[1]

        return f"http://localhost:{self.port}"

    async def stop(self) -> None:
        """Stop the mock server."""
        if self.server:
            self.server.close()
            await self.server.wait_closed()


class ProperServer:
    """Server that properly indicates connection closure with Connection: close header."""

    def __init__(self) -> None:
        self.request_count: int = 0
        self.app: web.Application = web.Application()
        self.app.router.add_post("/onvif/device_service", self.handle_request)
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None

    async def handle_request(self, request: web.Request) -> web.Response:
        """Handle request and properly indicate connection will close."""
        self.request_count += 1
        await request.read()

        # Properly indicate connection will close
        return web.Response(
            body=f"<response>{self.request_count}</response>".encode(),
            content_type="application/soap+xml",
            headers={"Connection": "close"},
        )

    async def start(self, port: int = 8889) -> str:
        """Start the server."""
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "localhost", port)
        await self.site.start()
        return f"http://localhost:{port}"

    async def stop(self) -> None:
        """Stop the server."""
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()


@pytest_asyncio.fixture
async def disconnecting_server() -> AsyncGenerator[tuple[DisconnectingServer, str]]:
    """Fixture that provides a server that disconnects without notice."""
    server = DisconnectingServer()
    base_url = await server.start()
    yield server, base_url
    await server.stop()


@pytest_asyncio.fixture
async def proper_server() -> AsyncGenerator[tuple[ProperServer, str]]:
    """Fixture that provides a server that properly sends Connection: close."""
    server = ProperServer()
    base_url = await server.start()
    yield server, base_url
    await server.stop()


@pytest.mark.asyncio
async def test_retry_on_server_disconnect_with_mock_server(
    disconnecting_server: tuple[DisconnectingServer, str],
) -> None:
    """Test that the client retries when server closes connection without notice."""

    server, base_url = disconnecting_server

    # Create client session with connection pooling
    # This will try to reuse connections
    async with ClientSession() as session:
        transport = AsyncTransportProtocolErrorHandler(
            session=session, verify_ssl=False
        )

        # Create real XML envelope
        envelope = etree.Element(
            "{http://www.w3.org/2003/05/soap-envelope}Envelope",
            nsmap={
                "soap-env": "http://www.w3.org/2003/05/soap-envelope",
                "ns0": "http://www.onvif.org/ver10/device/wsdl",
            },
        )
        body = etree.SubElement(
            envelope, "{http://www.w3.org/2003/05/soap-envelope}Body"
        )
        etree.SubElement(
            body, "{http://www.onvif.org/ver10/device/wsdl}GetDeviceInformation"
        )

        # First request should succeed
        result1 = await transport.post_xml(
            f"{base_url}/onvif/device_service", envelope, {}
        )
        assert result1.status_code == 200
        assert b"RequestNumber>1<" in result1._content

        # Small delay to ensure connection is fully closed
        await asyncio.sleep(0.02)

        # Second request will fail initially due to closed connection
        # but should succeed on retry
        result2 = await transport.post_xml(
            f"{base_url}/onvif/device_service", envelope, {}
        )
        assert result2.status_code == 200

        # Should have made 3 total requests (1st success, 2nd fail, 3rd retry success)
        # Note: The exact behavior depends on connection pooling timing
        assert server.request_count >= 2


@pytest.mark.asyncio
async def test_multiple_sequential_requests_with_disconnects(
    disconnecting_server: tuple[DisconnectingServer, str],
) -> None:
    """Sequential requests against a server that closes after each response.

    The transport retries only the pre-write disconnect cases
    (ServerDisconnectedError, ClientConnectionResetError) because those mean
    the request bytes never reached the server. A mid-read kernel RST
    (ClientOSError) can still surface when the client's response read races
    the server's close, and we deliberately do not auto-retry that case to
    avoid double-executing non-idempotent operations. The test therefore
    tolerates a small number of ClientOSError failures and asserts the
    transport stays usable across the iterations.
    """

    server, base_url = disconnecting_server

    async with ClientSession() as session:
        transport = AsyncTransportProtocolErrorHandler(
            session=session, verify_ssl=False
        )

        envelope = etree.Element("{http://test}TestRequest")

        successes = 0
        for _ in range(5):
            await asyncio.sleep(0)
            try:
                result = await transport.post_xml(
                    f"{base_url}/onvif/device_service", envelope, {}
                )
            except aiohttp.ClientOSError:
                continue
            assert result.status_code == 200
            successes += 1

        # Most iterations should succeed; the transport must stay usable even
        # when a few mid-read RSTs surface. server.request_count covers retries
        # too, so it can exceed the success count.
        assert successes >= 3
        assert server.request_count >= successes


@pytest.mark.asyncio
async def test_no_retry_with_proper_connection_close(
    proper_server: tuple[ProperServer, str],
) -> None:
    """Test that no retry occurs when server properly sends Connection: close."""

    server, base_url = proper_server

    async with ClientSession() as session:
        transport = AsyncTransportProtocolErrorHandler(
            session=session, verify_ssl=False
        )

        # Create simple test envelope
        envelope = etree.Element("{http://test}TestRequest")

        # Make 3 requests - no retries should occur
        for _ in range(3):
            result = await transport.post_xml(
                f"{base_url}/onvif/device_service", envelope, {}
            )
            assert result.status_code == 200

        # Should be exactly 3 requests (no retries)
        assert server.request_count == 3


@pytest.mark.asyncio
@pytest.mark.usefixtures("mock_etree_to_string")
async def test_post_xml_without_retry_decorator_fails() -> None:
    """Test that without the retry decorator on post_xml, ServerDisconnectedError propagates."""

    # Create a mock session
    mock_session = Mock(spec=ClientSession)
    mock_session.timeout = Mock(total=30, sock_read=10)

    # Create the base transport (without retry decorator)
    transport = AIOHTTPTransport(session=mock_session, verify_ssl=False)

    # Mock envelope
    mock_envelope = Mock()
    mock_envelope.tag = "TestEnvelope"

    # Make session.post raise ServerDisconnectedError
    mock_session.post = AsyncMock(
        side_effect=aiohttp.ServerDisconnectedError("Server disconnected")
    )

    # This should raise ServerDisconnectedError without retry
    with pytest.raises(aiohttp.ServerDisconnectedError):
        await transport.post_xml("http://example.com/onvif", mock_envelope, {})

    # Should only be called once (no retry)
    assert mock_session.post.call_count == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("mock_etree_to_string")
async def test_post_xml_with_retry_decorator_succeeds() -> None:
    """Test that with the retry decorator on post_xml, ServerDisconnectedError is retried."""

    # Create a mock session
    mock_session = Mock(spec=ClientSession)
    mock_session.timeout = Mock(total=30, sock_read=10)

    # Create the transport with retry decorator
    transport = AsyncTransportProtocolErrorHandler(
        session=mock_session, verify_ssl=False
    )

    # Mock envelope
    mock_envelope = Mock()
    mock_envelope.tag = "TestEnvelope"

    # First call fails, second succeeds
    mock_response = Mock()
    mock_response.status = 200
    mock_response.headers = {}
    mock_response.cookies = {}
    mock_response.charset = "utf-8"
    mock_response.read = AsyncMock(return_value=b"<response/>")

    mock_session.post = AsyncMock(
        side_effect=[
            aiohttp.ServerDisconnectedError("Server disconnected"),
            mock_response,
        ]
    )

    # This should succeed after retry
    result = await transport.post_xml("http://example.com/onvif", mock_envelope, {})

    # Should be called twice (initial + retry)
    assert mock_session.post.call_count == 2
    assert result.status_code == 200


@pytest.mark.asyncio
@pytest.mark.usefixtures("mock_etree_to_string")
async def test_post_xml_retries_on_client_connection_reset() -> None:
    """ClientConnectionResetError ('Cannot write to closing transport') is retried.

    aiohttp raises this when the pooled socket is closing before any bytes
    are written, so the request has not reached the server and a retry is
    idempotency-safe (sibling case to ServerDisconnectedError).
    """
    mock_session = Mock(spec=ClientSession)
    mock_session.timeout = Mock(total=30, sock_read=10)
    transport = AsyncTransportProtocolErrorHandler(
        session=mock_session, verify_ssl=False
    )

    mock_envelope = Mock()
    mock_envelope.tag = "TestEnvelope"

    mock_response = Mock()
    mock_response.status = 200
    mock_response.headers = {}
    mock_response.cookies = {}
    mock_response.charset = "utf-8"
    mock_response.read = AsyncMock(return_value=b"<response/>")

    mock_session.post = AsyncMock(
        side_effect=[
            aiohttp.ClientConnectionResetError("Cannot write to closing transport"),
            mock_response,
        ]
    )

    result = await transport.post_xml("http://example.com/onvif", mock_envelope, {})

    assert mock_session.post.call_count == 2
    assert result.status_code == 200


@pytest.mark.asyncio
@pytest.mark.usefixtures("mock_etree_to_string")
async def test_post_xml_does_not_retry_on_client_os_error() -> None:
    """Mid-read ClientOSError is not retried because bytes already went out.

    Retrying after the server may have processed the request risks duplicate
    Subscribe calls or PullMessages losing the in-flight event batch.
    """
    mock_session = Mock(spec=ClientSession)
    mock_session.timeout = Mock(total=30, sock_read=10)
    transport = AsyncTransportProtocolErrorHandler(
        session=mock_session, verify_ssl=False
    )

    mock_envelope = Mock()
    mock_envelope.tag = "TestEnvelope"

    mock_session.post = AsyncMock(
        side_effect=aiohttp.ClientOSError("Connection reset by peer")
    )

    with pytest.raises(aiohttp.ClientOSError, match="Connection reset by peer"):
        await transport.post_xml("http://example.com/onvif", mock_envelope, {})

    assert mock_session.post.call_count == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("mock_etree_to_string")
async def test_post_xml_decorator_is_applied() -> None:
    """Verify that the post_xml method has the retry decorator applied."""

    # Check that AsyncTransportProtocolErrorHandler.post_xml has the decorator

    # The decorated function will have been wrapped
    # Check if the function has the expected decorator behavior
    mock_session = Mock(spec=ClientSession)
    mock_session.timeout = Mock(total=30, sock_read=10)

    transport = AsyncTransportProtocolErrorHandler(
        session=mock_session, verify_ssl=False
    )

    # Get the actual method
    post_xml_method = transport.post_xml

    # Check if it's wrapped (the wrapper will have different attributes than the original)
    # The retry decorator wrapper should be a coroutine
    assert inspect.iscoroutinefunction(post_xml_method)

    # Verify it actually retries by testing with ServerDisconnectedError
    mock_envelope = Mock()
    mock_envelope.tag = "TestEnvelope"

    # Set up to fail twice (max retries)
    mock_session.post = AsyncMock(
        side_effect=aiohttp.ServerDisconnectedError("Server disconnected")
    )

    # Should raise after 2 attempts
    with pytest.raises(aiohttp.ServerDisconnectedError):
        await transport.post_xml("http://example.com/onvif", mock_envelope, {})

    # Verify it was called exactly twice (2 attempts as configured)
    assert mock_session.post.call_count == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("mock_etree_to_string")
async def test_retry_only_for_server_disconnected() -> None:
    """Test that retry only happens for ServerDisconnectedError, not other exceptions."""

    mock_session = Mock(spec=ClientSession)
    mock_session.timeout = Mock(total=30, sock_read=10)

    transport = AsyncTransportProtocolErrorHandler(
        session=mock_session, verify_ssl=False
    )

    mock_envelope = Mock()
    mock_envelope.tag = "TestEnvelope"

    # Different error type should not retry
    mock_session.post = AsyncMock(side_effect=aiohttp.ClientError("Different error"))

    with pytest.raises(aiohttp.ClientError, match="Different error"):
        await transport.post_xml("http://example.com/onvif", mock_envelope, {})

    # Should only be called once (no retry for other errors)
    assert mock_session.post.call_count == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("mock_etree_to_string")
async def test_post_with_retry_decorator_succeeds() -> None:
    """Test that with the retry decorator on post, ServerDisconnectedError is retried."""

    # Create a mock session
    mock_session = Mock(spec=ClientSession)
    mock_session.timeout = Mock(total=30, sock_read=10)

    # Create the transport with retry decorator
    transport = AsyncTransportProtocolErrorHandler(
        session=mock_session, verify_ssl=False
    )

    # First call fails, second succeeds
    mock_response = Mock()
    mock_response.status = 200
    mock_response.headers = {}
    mock_response.cookies = {}
    mock_response.charset = "utf-8"
    mock_response.read = AsyncMock(return_value=b"<response/>")

    mock_session.post = AsyncMock(
        side_effect=[
            aiohttp.ServerDisconnectedError("Server disconnected"),
            mock_response,
        ]
    )

    # This should succeed after retry
    result = await transport.post("http://example.com/onvif", "<test/>", {})

    # Should be called twice (initial + retry)
    assert mock_session.post.call_count == 2
    assert result.status_code == 200


@pytest.mark.asyncio
async def test_get_with_retry_decorator_succeeds() -> None:
    """Test that with the retry decorator on get, ServerDisconnectedError is retried."""

    # Create a mock session
    mock_session = Mock(spec=ClientSession)
    mock_session.timeout = Mock(total=30, sock_read=10)

    # Create the transport with retry decorator
    transport = AsyncTransportProtocolErrorHandler(
        session=mock_session, verify_ssl=False
    )

    # First call fails, second succeeds
    mock_response = Mock()
    mock_response.status = 200
    mock_response.headers = {}
    mock_response.cookies = {}
    mock_response.charset = "utf-8"
    mock_response.read = AsyncMock(return_value=b"<response/>")

    mock_session.get = AsyncMock(
        side_effect=[
            aiohttp.ServerDisconnectedError("Server disconnected"),
            mock_response,
        ]
    )

    # This should succeed after retry
    result = await transport.get("http://example.com/onvif")

    # Should be called twice (initial + retry)
    assert mock_session.get.call_count == 2
    assert result.status_code == 200


@pytest.mark.asyncio
@pytest.mark.usefixtures("mock_etree_to_string")
async def test_post_without_retry_decorator_fails() -> None:
    """Test that without the retry decorator on post, ServerDisconnectedError propagates."""

    # Create a mock session
    mock_session = Mock(spec=ClientSession)
    mock_session.timeout = Mock(total=30, sock_read=10)

    # Create the base transport (without retry decorator)
    transport = AIOHTTPTransport(session=mock_session, verify_ssl=False)

    # Make session.post raise ServerDisconnectedError
    mock_session.post = AsyncMock(
        side_effect=aiohttp.ServerDisconnectedError("Server disconnected")
    )

    # This should raise ServerDisconnectedError without retry
    with pytest.raises(aiohttp.ServerDisconnectedError):
        await transport.post("http://example.com/onvif", "<test/>", {})

    # Should only be called once (no retry)
    assert mock_session.post.call_count == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("mock_etree_to_string")
@pytest.mark.parametrize("no_cache", [True, False])
async def test_onvif_service_retries_on_server_disconnect(no_cache: bool) -> None:
    """ONVIFService must retry ServerDisconnectedError regardless of WSDL caching.

    Regression test for issue #169: the default (no_cache=False) cached
    transport lost the disconnect retry during the aiohttp migration, so a
    ServerDisconnectedError surfaced to event listeners (PullMessages) instead
    of being retried as it was on the previous httpx-based release.
    """
    wsdl = str(Path(onvif.__file__).parent / "wsdl" / "devicemgmt.wsdl")
    service = ONVIFService(
        "http://example.com/onvif/device_service",
        "user",
        "pass",
        wsdl,
        no_cache=no_cache,
    )
    try:
        mock_session = Mock(spec=ClientSession)
        mock_session.timeout = Mock(total=30, sock_read=10)

        mock_response = Mock()
        mock_response.status = 200
        mock_response.headers = {}
        mock_response.cookies = {}
        mock_response.charset = "utf-8"
        mock_response.read = AsyncMock(return_value=b"<response/>")

        # First request fails with a disconnect, retry on a fresh connection
        # succeeds.
        mock_session.post = AsyncMock(
            side_effect=[
                aiohttp.ServerDisconnectedError("Server disconnected"),
                mock_response,
            ]
        )
        service.transport.session = mock_session
        service.transport._client_timeout = mock_session.timeout

        envelope = Mock()
        envelope.tag = "TestEnvelope"

        result = await service.transport.post_xml(
            "http://example.com/onvif", envelope, {}
        )

        # Retried once (initial failure + successful retry) for both cache modes.
        assert mock_session.post.call_count == 2
        assert result.status_code == 200
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_get_without_retry_decorator_fails() -> None:
    """Test that without the retry decorator on get, ServerDisconnectedError propagates."""

    # Create a mock session
    mock_session = Mock(spec=ClientSession)
    mock_session.timeout = Mock(total=30, sock_read=10)

    # Create the base transport (without retry decorator)
    transport = AIOHTTPTransport(session=mock_session, verify_ssl=False)

    # Make session.get raise ServerDisconnectedError
    mock_session.get = AsyncMock(
        side_effect=aiohttp.ServerDisconnectedError("Server disconnected")
    )

    # This should raise ServerDisconnectedError without retry
    with pytest.raises(aiohttp.ServerDisconnectedError):
        await transport.get("http://example.com/onvif")

    # Should only be called once (no retry)
    assert mock_session.get.call_count == 1
