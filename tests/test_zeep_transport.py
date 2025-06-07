"""Tests for AIOHTTPTransport to ensure compatibility with zeep's AsyncTransport."""

from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import httpx
import pytest
from lxml import etree
from onvif.zeep_aiohttp import AIOHTTPTransport
from requests import Response as RequestsResponse


def create_mock_session(timeout=None):
    """Create a mock aiohttp session with optional timeout."""
    mock_session = Mock(spec=aiohttp.ClientSession)
    if timeout:
        mock_session.timeout = timeout
    else:
        # Create a default timeout object
        default_timeout = Mock(total=300, sock_read=None)
        mock_session.timeout = default_timeout
    return mock_session


@pytest.mark.asyncio
async def test_post_returns_httpx_response():
    """Test that post() returns an httpx.Response object."""
    mock_session = create_mock_session()
    transport = AIOHTTPTransport(session=mock_session)

    # Mock aiohttp session and response
    mock_aiohttp_response = Mock(spec=aiohttp.ClientResponse)
    mock_aiohttp_response.status = 200
    mock_aiohttp_response.headers = {"Content-Type": "text/xml"}
    mock_aiohttp_response.method = "POST"
    mock_aiohttp_response.url = "http://example.com/service"
    mock_aiohttp_response.charset = "utf-8"
    mock_aiohttp_response.cookies = {}

    mock_content = b"<response>test</response>"
    mock_aiohttp_response.read = AsyncMock(return_value=mock_content)

    mock_session.post = AsyncMock(return_value=mock_aiohttp_response)

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
async def test_post_xml_returns_requests_response():
    """Test that post_xml() returns a requests.Response object."""
    mock_session = create_mock_session()
    transport = AIOHTTPTransport(session=mock_session)

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

    mock_session.post = AsyncMock(return_value=mock_aiohttp_response)

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
async def test_get_returns_requests_response():
    """Test that get() returns a requests.Response object."""
    mock_session = create_mock_session()
    transport = AIOHTTPTransport(session=mock_session)

    # Mock aiohttp session and response
    mock_aiohttp_response = Mock(spec=aiohttp.ClientResponse)
    mock_aiohttp_response.status = 200
    mock_aiohttp_response.headers = {"Content-Type": "text/xml"}
    mock_aiohttp_response.charset = "utf-8"
    mock_aiohttp_response.cookies = {}
    mock_aiohttp_response.raise_for_status = Mock()

    mock_content = b"<response>test</response>"
    mock_aiohttp_response.read = AsyncMock(return_value=mock_content)

    mock_session.get = AsyncMock(return_value=mock_aiohttp_response)

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
async def test_context_manager():
    """Test async context manager doesn't close provided session."""
    mock_session = create_mock_session()
    transport = AIOHTTPTransport(session=mock_session)

    # Session should already be set
    assert transport.session == mock_session

    async with transport:
        assert transport.session == mock_session

    # Session should still be there after context (not closed)
    assert transport.session == mock_session


@pytest.mark.asyncio
async def test_aclose():
    """Test aclose() method doesn't close provided session."""
    mock_session = Mock(spec=aiohttp.ClientSession)
    mock_session.timeout = Mock(total=300, sock_read=None)
    mock_session.close = AsyncMock()
    transport = AIOHTTPTransport(session=mock_session)

    # Call aclose
    await transport.aclose()

    # Verify session.close() was NOT called (we don't close provided sessions)
    mock_session.close.assert_not_called()


def test_load_sync():
    """Test load() method works synchronously."""
    mock_session = create_mock_session()
    transport = AIOHTTPTransport(session=mock_session)

    # Mock the async get method
    mock_response = Mock(spec=RequestsResponse)
    mock_response.content = b"<wsdl>test</wsdl>"

    with patch.object(transport, "get", new=AsyncMock(return_value=mock_response)):
        result = transport.load("http://example.com/wsdl")

    assert result == b"<wsdl>test</wsdl>"


@pytest.mark.asyncio
async def test_timeout_handling():
    """Test timeout errors are properly handled."""
    mock_session = create_mock_session(timeout=aiohttp.ClientTimeout(total=0.1))
    transport = AIOHTTPTransport(session=mock_session)

    # Mock session that times out
    mock_session = Mock(spec=aiohttp.ClientSession)
    mock_session.post = AsyncMock(side_effect=TimeoutError())

    transport.session = mock_session

    with pytest.raises(TimeoutError, match="Request to .* timed out"):
        await transport.post(
            "http://example.com/service", "<request>test</request>", {}
        )


@pytest.mark.asyncio
async def test_connection_error_handling():
    """Test connection errors are properly handled."""
    mock_session = create_mock_session()
    transport = AIOHTTPTransport(session=mock_session)

    # Mock session that fails
    mock_session = Mock(spec=aiohttp.ClientSession)
    mock_session.get = AsyncMock(side_effect=aiohttp.ClientError("Connection failed"))

    transport.session = mock_session

    with pytest.raises(aiohttp.ClientError, match="Connection failed"):
        await transport.get("http://example.com/wsdl")


@pytest.mark.asyncio
async def test_constructor_parameters():
    """Test constructor accepts expected parameters."""
    # Test with minimal parameters
    mock_session1 = create_mock_session()
    transport1 = AIOHTTPTransport(session=mock_session1)
    # Session's timeout should be used
    assert transport1.session.timeout == mock_session1.timeout
    assert transport1.verify_ssl is True
    assert transport1.proxy is None

    # Test with all parameters
    timeout = aiohttp.ClientTimeout(total=100, connect=20)
    mock_session2 = Mock(spec=aiohttp.ClientSession)
    mock_session2.timeout = timeout
    transport2 = AIOHTTPTransport(
        session=mock_session2,
        verify_ssl=False,
        proxy="http://proxy:8080",
    )
    assert transport2.session == mock_session2
    assert transport2.verify_ssl is False
    assert transport2.proxy == "http://proxy:8080"


@pytest.mark.asyncio
async def test_post_with_bytes_message():
    """Test post() handles bytes message correctly."""
    mock_session = create_mock_session()
    transport = AIOHTTPTransport(session=mock_session)

    # Mock response
    mock_aiohttp_response = Mock(spec=aiohttp.ClientResponse)
    mock_aiohttp_response.status = 200
    mock_aiohttp_response.headers = {"Content-Type": "text/xml"}
    mock_aiohttp_response.method = "POST"
    mock_aiohttp_response.url = "http://example.com"
    mock_aiohttp_response.charset = "utf-8"
    mock_aiohttp_response.cookies = {}
    mock_aiohttp_response.raise_for_status = Mock()
    mock_aiohttp_response.read = AsyncMock(return_value=b"<response/>")

    mock_session = Mock(spec=aiohttp.ClientSession)
    mock_session.post = AsyncMock(return_value=mock_aiohttp_response)
    transport.session = mock_session

    # Test with bytes message
    result = await transport.post(
        "http://example.com", b"<request/>", {"SOAPAction": "test"}
    )
    assert isinstance(result, httpx.Response)
    assert result.status_code == 200


@pytest.mark.asyncio
async def test_get_with_none_params():
    """Test get() works with None params and headers."""
    mock_session = create_mock_session()
    transport = AIOHTTPTransport(session=mock_session)

    # Mock response
    mock_aiohttp_response = Mock(spec=aiohttp.ClientResponse)
    mock_aiohttp_response.status = 200
    mock_aiohttp_response.headers = {"Content-Type": "text/xml"}
    mock_aiohttp_response.charset = "utf-8"
    mock_aiohttp_response.cookies = {}
    mock_aiohttp_response.raise_for_status = Mock()
    mock_aiohttp_response.read = AsyncMock(return_value=b"<wsdl/>")

    mock_session = Mock(spec=aiohttp.ClientSession)
    mock_session.get = AsyncMock(return_value=mock_aiohttp_response)
    transport.session = mock_session

    # Test without params/headers (should work with None)
    result = await transport.get("http://example.com/wsdl", None, None)
    assert isinstance(result, RequestsResponse)
    assert result.status_code == 200


@pytest.mark.asyncio
async def test_user_agent_header():
    """Test that User-Agent header is set correctly like AsyncTransport."""
    mock_session = create_mock_session()
    transport = AIOHTTPTransport(session=mock_session)

    mock_aiohttp_response = Mock(spec=aiohttp.ClientResponse)
    mock_aiohttp_response.status = 200
    mock_aiohttp_response.headers = {}
    mock_aiohttp_response.method = "POST"
    mock_aiohttp_response.url = "http://example.com"
    mock_aiohttp_response.charset = "utf-8"
    mock_aiohttp_response.cookies = {}
    mock_aiohttp_response.raise_for_status = Mock()
    mock_aiohttp_response.read = AsyncMock(return_value=b"test")

    mock_session = Mock(spec=aiohttp.ClientSession)
    post_mock = AsyncMock(return_value=mock_aiohttp_response)
    mock_session.post = post_mock
    transport.session = mock_session

    await transport.post("http://example.com", "test", {})

    # Check User-Agent was set
    call_args = post_mock.call_args
    headers = call_args[1]["headers"]
    assert "User-Agent" in headers
    assert headers["User-Agent"].startswith("Zeep/")


@pytest.mark.asyncio
async def test_custom_timeout_used():
    """Test custom timeout is used when set."""
    custom_timeout = aiohttp.ClientTimeout(total=10, connect=5)
    mock_session = create_mock_session(timeout=custom_timeout)
    transport = AIOHTTPTransport(session=mock_session)

    mock_aiohttp_response = Mock(spec=aiohttp.ClientResponse)
    mock_aiohttp_response.status = 200
    mock_aiohttp_response.headers = {}
    mock_aiohttp_response.method = "POST"
    mock_aiohttp_response.url = "http://example.com"
    mock_aiohttp_response.charset = "utf-8"
    mock_aiohttp_response.cookies = {}
    mock_aiohttp_response.raise_for_status = Mock()
    mock_aiohttp_response.read = AsyncMock(return_value=b"test")

    mock_session = Mock(spec=aiohttp.ClientSession)
    post_mock = AsyncMock(return_value=mock_aiohttp_response)
    mock_session.post = post_mock
    transport.session = mock_session

    await transport.post("http://example.com", "test", {})

    # Check that custom timeout was used
    call_args = post_mock.call_args
    timeout = call_args[1]["timeout"]
    assert timeout is not None
    assert timeout == custom_timeout


@pytest.mark.asyncio
async def test_proxy_parameter():
    """Test proxy parameter is passed correctly."""
    mock_session = create_mock_session()
    transport = AIOHTTPTransport(session=mock_session, proxy="http://proxy:8080")

    mock_aiohttp_response = Mock(spec=aiohttp.ClientResponse)
    mock_aiohttp_response.status = 200
    mock_aiohttp_response.headers = {}
    mock_aiohttp_response.method = "GET"
    mock_aiohttp_response.url = "http://example.com"
    mock_aiohttp_response.charset = "utf-8"
    mock_aiohttp_response.cookies = {}
    mock_aiohttp_response.raise_for_status = Mock()
    mock_aiohttp_response.read = AsyncMock(return_value=b"test")

    mock_session = Mock(spec=aiohttp.ClientSession)
    get_mock = AsyncMock(return_value=mock_aiohttp_response)
    mock_session.get = get_mock
    transport.session = mock_session

    await transport.get("http://example.com")

    # Check proxy was passed
    call_args = get_mock.call_args
    assert call_args[1]["proxy"] == "http://proxy:8080"


@pytest.mark.asyncio
async def test_verify_ssl_false():
    """Test verify_ssl=False is stored correctly."""
    mock_session = create_mock_session()
    transport = AIOHTTPTransport(session=mock_session, verify_ssl=False)

    # verify_ssl should be stored
    assert transport.verify_ssl is False


@pytest.mark.asyncio
async def test_verify_ssl_true():
    """Test verify_ssl=True is stored correctly."""
    mock_session = create_mock_session()
    transport = AIOHTTPTransport(session=mock_session, verify_ssl=True)

    # verify_ssl should be stored
    assert transport.verify_ssl is True


@pytest.mark.asyncio
async def test_response_encoding():
    """Test response encoding is properly handled."""
    mock_session = create_mock_session()
    transport = AIOHTTPTransport(session=mock_session)

    # Mock response with specific encoding
    mock_aiohttp_response = Mock(spec=aiohttp.ClientResponse)
    mock_aiohttp_response.status = 200
    mock_aiohttp_response.headers = {"Content-Type": "text/xml; charset=iso-8859-1"}
    mock_aiohttp_response.charset = "iso-8859-1"
    mock_aiohttp_response.cookies = {}
    mock_aiohttp_response.raise_for_status = Mock()
    mock_aiohttp_response.read = AsyncMock(return_value=b"test")

    mock_session = Mock(spec=aiohttp.ClientSession)
    mock_session.get = AsyncMock(return_value=mock_aiohttp_response)
    transport.session = mock_session

    result = await transport.get("http://example.com")

    # Check encoding was preserved
    assert result.encoding == "iso-8859-1"


@pytest.mark.asyncio
async def test_cookies_in_httpx_response():
    """Test cookies are properly transferred to httpx response."""
    mock_session = create_mock_session()
    transport = AIOHTTPTransport(session=mock_session)

    # Mock cookies
    mock_cookie = Mock()
    mock_cookie.value = "abc123"
    mock_cookie.get.side_effect = lambda k: {"domain": ".example.com", "path": "/"}.get(
        k
    )

    mock_cookies = Mock()
    mock_cookies.items.return_value = [("session", mock_cookie)]

    # Mock response with cookies
    mock_aiohttp_response = Mock(spec=aiohttp.ClientResponse)
    mock_aiohttp_response.status = 200
    mock_aiohttp_response.headers = {}
    mock_aiohttp_response.method = "POST"
    mock_aiohttp_response.url = "http://example.com"
    mock_aiohttp_response.charset = "utf-8"
    mock_aiohttp_response.cookies = mock_cookies
    mock_aiohttp_response.raise_for_status = Mock()
    mock_aiohttp_response.read = AsyncMock(return_value=b"test")

    mock_session = Mock(spec=aiohttp.ClientSession)
    mock_session.post = AsyncMock(return_value=mock_aiohttp_response)
    transport.session = mock_session

    # Test httpx response (from post)
    httpx_result = await transport.post("http://example.com", "test", {})
    assert "session" in httpx_result.cookies


@pytest.mark.asyncio
async def test_cookies_in_requests_response():
    """Test cookies are properly transferred to requests response."""
    mock_session = create_mock_session()
    transport = AIOHTTPTransport(session=mock_session)

    # Mock cookies using SimpleCookie format
    from http.cookies import SimpleCookie

    mock_cookies = SimpleCookie()
    mock_cookies["session"] = "abc123"

    # Mock response with cookies
    mock_aiohttp_response = Mock(spec=aiohttp.ClientResponse)
    mock_aiohttp_response.status = 200
    mock_aiohttp_response.headers = {}
    mock_aiohttp_response.charset = "utf-8"
    mock_aiohttp_response.cookies = mock_cookies
    mock_aiohttp_response.raise_for_status = Mock()
    mock_aiohttp_response.read = AsyncMock(return_value=b"test")

    mock_session = Mock(spec=aiohttp.ClientSession)
    mock_session.get = AsyncMock(return_value=mock_aiohttp_response)
    transport.session = mock_session

    # Test requests response (from get)
    requests_result = await transport.get("http://example.com")
    assert "session" in requests_result.cookies
    assert requests_result.cookies["session"] == "abc123"


@pytest.mark.asyncio
async def test_inherited_transport_attributes():
    """Test that Transport base class attributes are available."""
    mock_session = create_mock_session()
    transport = AIOHTTPTransport(session=mock_session)

    # Should have logger attribute from Transport
    assert hasattr(transport, "logger")

    # Should have cache attribute (though we set it to None)
    assert hasattr(transport, "cache")
    assert transport.cache is None

    # Should have operation_timeout attribute from parent
    assert hasattr(transport, "operation_timeout")
    assert transport.operation_timeout is None


@pytest.mark.asyncio
async def test_session_reuse():
    """Test transport reuses provided session."""
    mock_session = create_mock_session()
    transport = AIOHTTPTransport(session=mock_session)

    # Mock response
    mock_aiohttp_response = Mock(spec=aiohttp.ClientResponse)
    mock_aiohttp_response.status = 200
    mock_aiohttp_response.headers = {}
    mock_aiohttp_response.charset = "utf-8"
    mock_aiohttp_response.cookies = {}
    mock_aiohttp_response.raise_for_status = Mock()
    mock_aiohttp_response.read = AsyncMock(return_value=b"test")

    mock_session.get = AsyncMock(return_value=mock_aiohttp_response)

    # Make multiple requests
    result1 = await transport.get("http://example.com")
    result2 = await transport.get("http://example.com")

    assert result1.content == b"test"
    assert result2.content == b"test"

    # Session should be reused
    assert mock_session.get.call_count == 2


def test_sync_load_creates_new_loop():
    """Test load() creates new event loop when called from async context."""
    mock_session = create_mock_session()
    transport = AIOHTTPTransport(session=mock_session)

    # Mock response
    mock_response = Mock(spec=RequestsResponse)
    mock_response.content = b"<wsdl/>"

    # This should work even if there's already an event loop
    with patch.object(transport, "get", new=AsyncMock(return_value=mock_response)):
        with patch("asyncio.new_event_loop") as mock_new_loop:
            mock_loop = Mock()
            mock_loop.run_until_complete.return_value = mock_response
            mock_new_loop.return_value = mock_loop

            result = transport.load("http://example.com/wsdl")

            # Should have created new loop
            mock_new_loop.assert_called_once()
            mock_loop.close.assert_called_once()
            assert result == b"<wsdl/>"


@pytest.mark.asyncio
async def test_content_type_header_default():
    """Test default Content-Type header is set for POST."""
    mock_session = create_mock_session()
    transport = AIOHTTPTransport(session=mock_session)

    mock_aiohttp_response = Mock(spec=aiohttp.ClientResponse)
    mock_aiohttp_response.status = 200
    mock_aiohttp_response.headers = {}
    mock_aiohttp_response.method = "POST"
    mock_aiohttp_response.url = "http://example.com"
    mock_aiohttp_response.charset = "utf-8"
    mock_aiohttp_response.cookies = {}
    mock_aiohttp_response.raise_for_status = Mock()
    mock_aiohttp_response.read = AsyncMock(return_value=b"test")

    mock_session = Mock(spec=aiohttp.ClientSession)
    post_mock = AsyncMock(return_value=mock_aiohttp_response)
    mock_session.post = post_mock
    transport.session = mock_session

    await transport.post("http://example.com", "test", {})

    # Check Content-Type was set
    call_args = post_mock.call_args
    headers = call_args[1]["headers"]
    assert headers["Content-Type"] == 'text/xml; charset="utf-8"'


@pytest.mark.asyncio
async def test_provided_session_not_closed():
    """Test that provided session is not closed by context manager."""
    mock_session = Mock(spec=aiohttp.ClientSession)
    mock_session.close = AsyncMock()

    transport = AIOHTTPTransport(session=mock_session)

    async with transport:
        assert transport.session == mock_session

    # Provided session should not be closed
    mock_session.close.assert_not_called()
    assert transport.session == mock_session


@pytest.mark.asyncio
async def test_cookie_conversion_httpx_basic():
    """Test basic cookie conversion from aiohttp to httpx response."""
    mock_session = create_mock_session()
    transport = AIOHTTPTransport(session=mock_session)

    # Create aiohttp cookies
    from http.cookies import SimpleCookie

    cookies = SimpleCookie()
    cookies["session"] = "abc123"
    cookies["session"]["domain"] = ".example.com"
    cookies["session"]["path"] = "/api"
    cookies["session"]["secure"] = True
    cookies["session"]["httponly"] = True
    cookies["session"]["max-age"] = "3600"

    cookies["user"] = "john_doe"
    cookies["user"]["domain"] = "example.com"
    cookies["user"]["path"] = "/"

    # Mock aiohttp response
    mock_response = Mock(spec=aiohttp.ClientResponse)
    mock_response.status = 200
    mock_response.headers = {}
    mock_response.method = "POST"
    mock_response.url = "http://example.com"
    mock_response.charset = "utf-8"
    mock_response.cookies = cookies
    mock_response.raise_for_status = Mock()
    mock_response.read = AsyncMock(return_value=b"test")

    mock_session = Mock(spec=aiohttp.ClientSession)
    mock_session.post = AsyncMock(return_value=mock_response)
    transport.session = mock_session

    # Make request
    result = await transport.post("http://example.com", "test", {})

    # Verify cookies in httpx response
    assert "session" in result.cookies
    assert result.cookies["session"] == "abc123"
    assert "user" in result.cookies
    assert result.cookies["user"] == "john_doe"


@pytest.mark.asyncio
async def test_cookie_conversion_requests_basic():
    """Test basic cookie conversion from aiohttp to requests response."""
    mock_session = create_mock_session()
    transport = AIOHTTPTransport(session=mock_session)

    # Create aiohttp cookies
    from http.cookies import SimpleCookie

    cookies = SimpleCookie()
    cookies["token"] = "xyz789"
    cookies["token"]["domain"] = ".api.example.com"
    cookies["token"]["path"] = "/v1"
    cookies["token"]["secure"] = True

    # Mock aiohttp response
    mock_response = Mock(spec=aiohttp.ClientResponse)
    mock_response.status = 200
    mock_response.headers = {}
    mock_response.charset = "utf-8"
    mock_response.cookies = cookies
    mock_response.raise_for_status = Mock()
    mock_response.read = AsyncMock(return_value=b"test")

    mock_session = Mock(spec=aiohttp.ClientSession)
    mock_session.get = AsyncMock(return_value=mock_response)
    transport.session = mock_session

    # Make request
    result = await transport.get("http://api.example.com/v1/data")

    # Verify cookies in requests response
    assert "token" in result.cookies
    assert result.cookies["token"] == "xyz789"


@pytest.mark.asyncio
async def test_cookie_attributes_httpx():
    """Test that cookie attributes are properly preserved in httpx response."""
    mock_session = create_mock_session()
    transport = AIOHTTPTransport(session=mock_session)

    # Create cookie with all attributes
    from http.cookies import SimpleCookie

    cookies = SimpleCookie()
    cookies["auth"] = "secret123"
    cookies["auth"]["domain"] = ".secure.com"
    cookies["auth"]["path"] = "/admin"
    cookies["auth"]["secure"] = True
    cookies["auth"]["httponly"] = True
    cookies["auth"]["samesite"] = "Strict"
    cookies["auth"]["max-age"] = "7200"

    # Mock response
    mock_response = Mock(spec=aiohttp.ClientResponse)
    mock_response.status = 200
    mock_response.headers = {}
    mock_response.method = "POST"
    mock_response.url = "https://secure.com/admin"
    mock_response.charset = "utf-8"
    mock_response.cookies = cookies
    mock_response.raise_for_status = Mock()
    mock_response.read = AsyncMock(return_value=b"secure")

    mock_session = Mock(spec=aiohttp.ClientSession)
    mock_session.post = AsyncMock(return_value=mock_response)
    transport.session = mock_session

    # Make request
    result = await transport.post("https://secure.com/admin", "login", {})

    # Check cookie exists
    assert "auth" in result.cookies
    assert result.cookies["auth"] == "secret123"

    # Note: httpx.Cookies doesn't expose all attributes directly,
    # but they should be preserved internally for cookie jar operations


@pytest.mark.asyncio
async def test_multiple_cookies():
    """Test handling multiple cookies."""
    mock_session = create_mock_session()
    transport = AIOHTTPTransport(session=mock_session)

    # Create multiple cookies
    from http.cookies import SimpleCookie

    cookies = SimpleCookie()
    for i in range(5):
        cookie_name = f"cookie{i}"
        cookies[cookie_name] = f"value{i}"
        cookies[cookie_name]["domain"] = ".example.com"
        cookies[cookie_name]["path"] = f"/path{i}"

    # Mock response
    mock_response = Mock(spec=aiohttp.ClientResponse)
    mock_response.status = 200
    mock_response.headers = {}
    mock_response.method = "GET"
    mock_response.url = "http://example.com"
    mock_response.charset = "utf-8"
    mock_response.cookies = cookies
    mock_response.raise_for_status = Mock()
    mock_response.read = AsyncMock(return_value=b"multi")

    mock_session = Mock(spec=aiohttp.ClientSession)
    mock_session.get = AsyncMock(return_value=mock_response)
    transport.session = mock_session

    # Make request
    result = await transport.get("http://example.com")

    # Verify all cookies
    for i in range(5):
        cookie_name = f"cookie{i}"
        assert cookie_name in result.cookies
        assert result.cookies[cookie_name] == f"value{i}"


@pytest.mark.asyncio
async def test_empty_cookies():
    """Test handling when no cookies are present."""
    mock_session = create_mock_session()
    transport = AIOHTTPTransport(session=mock_session)

    # Mock response without cookies
    from http.cookies import SimpleCookie

    mock_response = Mock(spec=aiohttp.ClientResponse)
    mock_response.status = 200
    mock_response.headers = {}
    mock_response.method = "GET"
    mock_response.url = "http://example.com"
    mock_response.charset = "utf-8"
    mock_response.cookies = SimpleCookie()  # Empty cookies
    mock_response.raise_for_status = Mock()
    mock_response.read = AsyncMock(return_value=b"nocookies")

    mock_session = Mock(spec=aiohttp.ClientSession)
    mock_session.get = AsyncMock(return_value=mock_response)
    transport.session = mock_session

    # Make request
    result = await transport.get("http://example.com")

    # Verify empty cookies
    assert len(result.cookies) == 0


@pytest.mark.asyncio
async def test_cookie_encoding():
    """Test cookies with special characters."""
    mock_session = create_mock_session()
    transport = AIOHTTPTransport(session=mock_session)

    # Create cookies with special chars
    from http.cookies import SimpleCookie

    cookies = SimpleCookie()
    cookies["data"] = "hello%20world%21"  # URL encoded
    cookies["unicode"] = "café"

    # Mock response
    mock_response = Mock(spec=aiohttp.ClientResponse)
    mock_response.status = 200
    mock_response.headers = {}
    mock_response.charset = "utf-8"
    mock_response.cookies = cookies
    mock_response.raise_for_status = Mock()
    mock_response.read = AsyncMock(return_value=b"encoded")

    mock_session = Mock(spec=aiohttp.ClientSession)
    mock_session.get = AsyncMock(return_value=mock_response)
    transport.session = mock_session

    # Make request
    result = await transport.get("http://example.com")

    # Verify encoded cookies
    assert "data" in result.cookies
    assert result.cookies["data"] == "hello%20world%21"
    assert "unicode" in result.cookies
    assert result.cookies["unicode"] == "café"


@pytest.mark.asyncio
async def test_cookie_jar_type():
    """Test that cookies are stored in appropriate jar types."""
    mock_session = create_mock_session()
    transport = AIOHTTPTransport(session=mock_session)

    from http.cookies import SimpleCookie

    cookies = SimpleCookie()
    cookies["test"] = "value"

    # Mock response
    mock_response = Mock(spec=aiohttp.ClientResponse)
    mock_response.status = 200
    mock_response.headers = {}
    mock_response.method = "POST"
    mock_response.url = "http://example.com"
    mock_response.charset = "utf-8"
    mock_response.cookies = cookies
    mock_response.raise_for_status = Mock()
    mock_response.read = AsyncMock(return_value=b"jar")

    mock_session = Mock(spec=aiohttp.ClientSession)
    mock_session.post = AsyncMock(return_value=mock_response)
    transport.session = mock_session

    # Test httpx response
    httpx_result = await transport.post("http://example.com", "test", {})
    assert isinstance(httpx_result.cookies, httpx.Cookies)

    # Test requests response
    mock_session.get = AsyncMock(return_value=mock_response)
    requests_result = await transport.get("http://example.com")
    # Verify cookies are accessible in requests response
    assert hasattr(requests_result.cookies, "__getitem__")
    assert "test" in requests_result.cookies


@pytest.mark.asyncio
async def test_http_error_responses_no_exception():
    """Test that HTTP error responses (401, 500, etc.) don't raise exceptions."""
    mock_session = create_mock_session()
    transport = AIOHTTPTransport(session=mock_session)

    # Test 401 Unauthorized
    mock_401_response = Mock(spec=aiohttp.ClientResponse)
    mock_401_response.status = 401
    mock_401_response.headers = {"Content-Type": "text/xml"}
    mock_401_response.method = "POST"
    mock_401_response.url = "http://example.com/service"
    mock_401_response.charset = "utf-8"
    mock_401_response.cookies = {}
    mock_401_response.read = AsyncMock(return_value=b"<error>Unauthorized</error>")

    mock_session = Mock(spec=aiohttp.ClientSession)
    mock_session.post = AsyncMock(return_value=mock_401_response)
    transport.session = mock_session

    # Should not raise exception
    result = await transport.post("http://example.com/service", "<request/>", {})
    assert isinstance(result, httpx.Response)
    assert result.status_code == 401
    assert result.read() == b"<error>Unauthorized</error>"

    # Test 500 Internal Server Error
    mock_500_response = Mock(spec=aiohttp.ClientResponse)
    mock_500_response.status = 500
    mock_500_response.headers = {"Content-Type": "text/xml"}
    mock_500_response.charset = "utf-8"
    mock_500_response.cookies = {}
    mock_500_response.read = AsyncMock(return_value=b"<error>Server Error</error>")

    mock_session.get = AsyncMock(return_value=mock_500_response)

    # Should not raise exception
    result = await transport.get("http://example.com/wsdl")
    assert isinstance(result, RequestsResponse)
    assert result.status_code == 500
    assert result.content == b"<error>Server Error</error>"
