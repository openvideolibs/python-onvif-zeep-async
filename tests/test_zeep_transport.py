"""Tests for AIOHTTPTransport to ensure compatibility with zeep's AsyncTransport."""

from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import httpx
import pytest
from lxml import etree
from onvif.zeep_aiohttp import AIOHTTPTransport
from requests import Response as RequestsResponse


class TestAIOHTTPTransport:
    """Test AIOHTTPTransport matches AsyncTransport behavior."""

    @pytest.mark.asyncio
    async def test_post_returns_httpx_response(self):
        """Test that post() returns an httpx.Response object."""
        transport = AIOHTTPTransport()

        # Mock aiohttp session and response
        mock_aiohttp_response = Mock(spec=aiohttp.ClientResponse)
        mock_aiohttp_response.status = 200
        mock_aiohttp_response.headers = {"Content-Type": "text/xml"}
        mock_aiohttp_response.method = "POST"
        mock_aiohttp_response.url = "http://example.com/service"
        mock_aiohttp_response.charset = "utf-8"
        mock_aiohttp_response.cookies = {}
        mock_aiohttp_response.raise_for_status = Mock()

        mock_content = b"<response>test</response>"
        mock_aiohttp_response.read = AsyncMock(return_value=mock_content)

        mock_session = Mock(spec=aiohttp.ClientSession)
        mock_session.post = AsyncMock(return_value=mock_aiohttp_response)

        transport.session = mock_session

        # Call post
        result = await transport.post(
            "http://example.com/service",
            "<request>test</request>",
            {"SOAPAction": "test"},
        )

        # Verify result is httpx.Response
        assert isinstance(result, httpx.Response)
        assert result.status_code == 200
        assert result.read() == mock_content

    @pytest.mark.asyncio
    async def test_post_xml_returns_requests_response(self):
        """Test that post_xml() returns a requests.Response object."""
        transport = AIOHTTPTransport()

        # Mock aiohttp session and response
        mock_aiohttp_response = Mock(spec=aiohttp.ClientResponse)
        mock_aiohttp_response.status = 200
        mock_aiohttp_response.headers = {"Content-Type": "text/xml"}
        mock_aiohttp_response.method = "POST"
        mock_aiohttp_response.url = "http://example.com/service"
        mock_aiohttp_response.charset = "utf-8"
        mock_aiohttp_response.cookies = {}
        mock_aiohttp_response.raise_for_status = Mock()

        mock_content = b"<response>test</response>"
        mock_aiohttp_response.read = AsyncMock(return_value=mock_content)

        mock_session = Mock(spec=aiohttp.ClientSession)
        mock_session.post = AsyncMock(return_value=mock_aiohttp_response)

        transport.session = mock_session

        # Create XML envelope
        envelope = etree.Element("Envelope")
        body = etree.SubElement(envelope, "Body")
        etree.SubElement(body, "Request").text = "test"

        # Call post_xml
        result = await transport.post_xml(
            "http://example.com/service", envelope, {"SOAPAction": "test"}
        )

        # Verify result is requests.Response
        assert isinstance(result, RequestsResponse)
        assert result.status_code == 200
        assert result.content == mock_content

    @pytest.mark.asyncio
    async def test_get_returns_requests_response(self):
        """Test that get() returns a requests.Response object."""
        transport = AIOHTTPTransport()

        # Mock aiohttp session and response
        mock_aiohttp_response = Mock(spec=aiohttp.ClientResponse)
        mock_aiohttp_response.status = 200
        mock_aiohttp_response.headers = {"Content-Type": "text/xml"}
        mock_aiohttp_response.charset = "utf-8"
        mock_aiohttp_response.cookies = {}
        mock_aiohttp_response.raise_for_status = Mock()

        mock_content = b"<response>test</response>"
        mock_aiohttp_response.read = AsyncMock(return_value=mock_content)

        mock_session = Mock(spec=aiohttp.ClientSession)
        mock_session.get = AsyncMock(return_value=mock_aiohttp_response)

        transport.session = mock_session

        # Call get
        result = await transport.get(
            "http://example.com/wsdl",
            params={"version": "1.0"},
            headers={"Accept": "text/xml"},
        )

        # Verify result is requests.Response
        assert isinstance(result, RequestsResponse)
        assert result.status_code == 200
        assert result.content == mock_content

    @pytest.mark.asyncio
    async def test_context_manager(self):
        """Test async context manager creates and closes session."""
        transport = AIOHTTPTransport()

        # Initial session should be None (not the parent's requests session)
        assert transport.session is None or not isinstance(
            transport.session, aiohttp.ClientSession
        )

        async with transport:
            assert transport.session is not None
            assert isinstance(transport.session, aiohttp.ClientSession)

        # Session should be closed after context
        assert transport.session is None

    @pytest.mark.asyncio
    async def test_aclose(self):
        """Test aclose() method closes the session."""
        transport = AIOHTTPTransport()

        # Create a mock session
        mock_session = Mock(spec=aiohttp.ClientSession)
        mock_session.close = AsyncMock()
        transport.session = mock_session

        # Call aclose
        await transport.aclose()

        # Verify session.close() was called
        mock_session.close.assert_called_once()

    def test_load_sync(self):
        """Test load() method works synchronously."""
        transport = AIOHTTPTransport()

        # Mock the async get method
        mock_response = Mock(spec=RequestsResponse)
        mock_response.content = b"<wsdl>test</wsdl>"

        with patch.object(transport, "get", new=AsyncMock(return_value=mock_response)):
            result = transport.load("http://example.com/wsdl")

        assert result == b"<wsdl>test</wsdl>"

    @pytest.mark.asyncio
    async def test_timeout_handling(self):
        """Test timeout errors are properly handled."""
        transport = AIOHTTPTransport(operation_timeout=0.1)

        # Mock session that times out
        mock_session = Mock(spec=aiohttp.ClientSession)
        mock_session.post = AsyncMock(side_effect=TimeoutError())

        transport.session = mock_session

        with pytest.raises(TimeoutError, match="Request to .* timed out"):
            await transport.post(
                "http://example.com/service", "<request>test</request>", {}
            )

    @pytest.mark.asyncio
    async def test_connection_error_handling(self):
        """Test connection errors are properly handled."""
        transport = AIOHTTPTransport()

        # Mock session that fails
        mock_session = Mock(spec=aiohttp.ClientSession)
        mock_session.get = AsyncMock(
            side_effect=aiohttp.ClientError("Connection failed")
        )

        transport.session = mock_session

        with pytest.raises(ConnectionError, match="Error connecting to"):
            await transport.get("http://example.com/wsdl")
