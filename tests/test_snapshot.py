"""Tests for snapshot functionality using aiohttp."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
import pytest_asyncio
import zeep.exceptions
from aiointercept import aiointercept
from yarl import URL

from onvif import ONVIFCamera
from onvif.exceptions import ONVIFAuthError, ONVIFError, ONVIFTimeoutError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from blockbuster import BlockBuster


@pytest_asyncio.fixture
async def mock_aioresponse(
    blockbuster: BlockBuster | None,
) -> AsyncGenerator[aiointercept]:
    """Return aiointercept fixture intercepting external camera URLs.

    aiointercept routes requests through a real local test server and intercepts
    them by patching aiohttp's DNS resolver. aiohttp bypasses the resolver for
    IP-literal hosts, so the mocked snapshot URIs use a hostname rather than an
    IP address.

    start()/stop() spin a background server thread up and down, blocking briefly
    on the synchronous handshake (threading.Event.wait / Thread.join / the
    module-level patch lock). Those are test-harness internals, not onvif code
    under test, so allow blocking inside aiointercept's lifecycle frames rather
    than letting blockbuster flag them.
    """
    if blockbuster is not None:
        blockbuster.functions["threading.Lock.acquire"].can_block_in(
            "aiointercept/core.py",
            {"start", "stop", "_start_server_thread", "_stop_server_thread"},
        )
    interceptor = aiointercept(mock_external_urls=True)
    await interceptor.start()
    try:
        yield interceptor
    finally:
        await interceptor.stop()


@asynccontextmanager
async def create_test_camera(
    host: str = "192.168.1.100",
    port: int = 80,
    user: str | None = "admin",
    passwd: str | None = "password",  # noqa: S107
) -> AsyncGenerator[ONVIFCamera]:
    """Create a test camera instance with context manager."""
    cam = ONVIFCamera(host, port, user, passwd)
    try:
        yield cam
    finally:
        await cam.close()


@pytest_asyncio.fixture
async def camera() -> AsyncGenerator[ONVIFCamera]:
    """Create a test camera instance."""
    async with create_test_camera() as cam:
        # Mock the device management service to avoid actual WSDL loading
        with (
            patch.object(cam, "create_devicemgmt_service", new_callable=AsyncMock),
            patch.object(
                cam, "create_media_service", new_callable=AsyncMock
            ) as mock_media,
        ):
            # Mock the media service to return snapshot URI
            mock_service = Mock()
            mock_service.create_type = Mock(return_value=Mock())
            mock_service.GetSnapshotUri = AsyncMock(
                return_value=Mock(Uri="http://camera.local/snapshot")
            )
            mock_media.return_value = mock_service
            yield cam


@pytest.mark.asyncio
async def test_get_snapshot_success_with_digest_auth(
    camera: ONVIFCamera, mock_aioresponse: aiointercept
) -> None:
    """Test successful snapshot retrieval with digest authentication."""
    snapshot_data = b"fake_image_data"

    # Mock successful response
    mock_aioresponse.get("http://camera.local/snapshot", status=200, body=snapshot_data)

    # Get snapshot with digest auth (default)
    result = await camera.get_snapshot("Profile1", basic_auth=False)

    assert result == snapshot_data

    # Check that the request was made
    assert len(mock_aioresponse.requests) == 1
    request_key = next(iter(mock_aioresponse.requests.keys()))
    assert str(request_key[1]).startswith("http://camera.local/snapshot")


@pytest.mark.asyncio
async def test_get_snapshot_success_with_basic_auth(
    camera: ONVIFCamera, mock_aioresponse: aiointercept
) -> None:
    """Test successful snapshot retrieval with basic authentication."""
    snapshot_data = b"fake_image_data"

    # Mock successful response
    mock_aioresponse.get("http://camera.local/snapshot", status=200, body=snapshot_data)

    # Get snapshot with basic auth
    result = await camera.get_snapshot("Profile1", basic_auth=True)

    assert result == snapshot_data

    # Check that the request was made
    assert len(mock_aioresponse.requests) == 1
    request_key = next(iter(mock_aioresponse.requests.keys()))
    assert str(request_key[1]).startswith("http://camera.local/snapshot")


@pytest.mark.asyncio
async def test_get_snapshot_auth_failure(
    camera: ONVIFCamera, mock_aioresponse: aiointercept
) -> None:
    """Test snapshot retrieval with authentication failure."""
    # Mock 401 response
    mock_aioresponse.get(
        "http://camera.local/snapshot", status=401, body=b"Unauthorized"
    )

    # Should raise ONVIFAuthError
    with pytest.raises(ONVIFAuthError) as exc_info:
        await camera.get_snapshot("Profile1")

    assert "Failed to authenticate" in str(exc_info.value)


@pytest.mark.asyncio
async def test_get_snapshot_with_user_pass_in_url(
    camera: ONVIFCamera, mock_aioresponse: aiointercept
) -> None:
    """Test snapshot retrieval when URI contains credentials."""
    # Mock the media service to return URI with credentials
    with patch.object(
        camera, "create_media_service", new_callable=AsyncMock
    ) as mock_media:
        mock_service = Mock()
        mock_service.create_type = Mock(return_value=Mock())
        mock_service.GetSnapshotUri = AsyncMock(
            return_value=Mock(Uri="http://admin:password@camera.local/snapshot")
        )
        mock_media.return_value = mock_service

        # aiohttp moves URL credentials into the Authorization header before the
        # request leaves the client, so both the credentialed first attempt and
        # the stripped retry are observed by the server (and queued/captured)
        # under the same userinfo-free URL. First attempt 401, retry succeeds.
        mock_aioresponse.get(
            "http://camera.local/snapshot", status=401, body=b"Unauthorized"
        )
        mock_aioresponse.get(
            "http://camera.local/snapshot", status=200, body=b"image_data"
        )

        result = await camera.get_snapshot("Profile1")

        assert result == b"image_data"
        # Should have made 2 requests - first with credentials in URL, second
        # without - both landing on the same stripped URL.
        request_key = ("GET", URL("http://camera.local/snapshot"))
        assert list(mock_aioresponse.requests.keys()) == [request_key]
        assert len(mock_aioresponse.requests[request_key]) == 2


@pytest.mark.asyncio
async def test_get_snapshot_timeout(camera: ONVIFCamera) -> None:
    """Test snapshot retrieval timeout."""
    # aiointercept routes requests through a real local server, so a client-side
    # timeout cannot be injected via the mock. Raise TimeoutError at the client
    # call to exercise the handle_snapshot_errors timeout branch directly.
    with (
        patch.object(
            camera._snapshot_client,
            "get",
            side_effect=TimeoutError("Connection timeout"),
        ),
        pytest.raises(ONVIFTimeoutError) as exc_info,
    ):
        await camera.get_snapshot("Profile1")

    assert "Timed out fetching" in str(exc_info.value)


@pytest.mark.asyncio
async def test_get_snapshot_client_error(
    camera: ONVIFCamera, mock_aioresponse: aiointercept
) -> None:
    """Test snapshot retrieval with client error."""
    # exception=True makes aiointercept drop the connection, raising an
    # aiohttp.ClientError subclass that handle_snapshot_errors maps to ONVIFError.
    mock_aioresponse.get("http://camera.local/snapshot", exception=True)

    with pytest.raises(ONVIFError) as exc_info:
        await camera.get_snapshot("Profile1")

    assert "Error fetching" in str(exc_info.value)


@pytest.mark.asyncio
async def test_get_snapshot_no_uri_available(camera: ONVIFCamera) -> None:
    """Test snapshot when no URI is available."""
    # Mock the media service to raise fault
    with patch.object(
        camera, "create_media_service", new_callable=AsyncMock
    ) as mock_media:
        mock_service = Mock()
        mock_service.create_type = Mock(return_value=Mock())

        mock_service.GetSnapshotUri = AsyncMock(
            side_effect=zeep.exceptions.Fault("Snapshot not supported")
        )
        mock_media.return_value = mock_service

        result = await camera.get_snapshot("Profile1")

        assert result is None


@pytest.mark.asyncio
async def test_get_snapshot_invalid_uri_response(camera: ONVIFCamera) -> None:
    """Test snapshot when device returns invalid URI."""
    # Mock the media service to return invalid response
    with patch.object(
        camera, "create_media_service", new_callable=AsyncMock
    ) as mock_media:
        mock_service = Mock()
        mock_service.create_type = Mock(return_value=Mock())
        # Return response without Uri attribute
        mock_service.GetSnapshotUri = AsyncMock(
            return_value=Mock(spec=[])  # No Uri attribute
        )
        mock_media.return_value = mock_service

        result = await camera.get_snapshot("Profile1")

        assert result is None


@pytest.mark.asyncio
async def test_get_snapshot_404_error(
    camera: ONVIFCamera, mock_aioresponse: aiointercept
) -> None:
    """Test snapshot retrieval with 404 error."""
    # Mock 404 response
    mock_aioresponse.get("http://camera.local/snapshot", status=404, body=b"Not Found")

    result = await camera.get_snapshot("Profile1")

    # Should return None for non-auth errors
    assert result is None


@pytest.mark.asyncio
async def test_get_snapshot_uri_caching(camera: ONVIFCamera) -> None:
    """Test that snapshot URI is cached after first retrieval."""
    # First call should fetch URI from service
    uri = await camera.get_snapshot_uri("Profile1")
    assert uri == "http://camera.local/snapshot"

    # Mock the media service to ensure it's not called again
    with patch.object(
        camera, "create_media_service", new_callable=AsyncMock
    ) as mock_media:
        mock_media.side_effect = Exception("Should not be called")

        # Second call should use cached URI
        uri2 = await camera.get_snapshot_uri("Profile1")
        assert uri2 == "http://camera.local/snapshot"

        # Mock media service should not have been called
        mock_media.assert_not_called()


@pytest.mark.asyncio
async def test_snapshot_client_session_reuse(
    camera: ONVIFCamera, mock_aioresponse: aiointercept
) -> None:
    """Test that snapshot client session is reused across requests."""
    snapshot_data = b"fake_image_data"

    # Get reference to the snapshot client
    snapshot_client = camera._snapshot_client

    # Mock multiple requests
    mock_aioresponse.get("http://camera.local/snapshot", status=200, body=snapshot_data)
    mock_aioresponse.get("http://camera.local/snapshot", status=200, body=snapshot_data)

    # Make multiple snapshot requests
    result1 = await camera.get_snapshot("Profile1")
    result2 = await camera.get_snapshot("Profile1")

    assert result1 == snapshot_data
    assert result2 == snapshot_data

    # Verify same client session was used
    assert camera._snapshot_client is snapshot_client


@pytest.mark.asyncio
async def test_get_snapshot_no_credentials(mock_aioresponse: aiointercept) -> None:
    """Test snapshot retrieval when camera has no credentials."""
    async with create_test_camera(user=None, passwd=None) as cam:
        with (
            patch.object(cam, "create_devicemgmt_service", new_callable=AsyncMock),
            patch.object(
                cam, "create_media_service", new_callable=AsyncMock
            ) as mock_media,
        ):
            mock_service = Mock()
            mock_service.create_type = Mock(return_value=Mock())
            mock_service.GetSnapshotUri = AsyncMock(
                return_value=Mock(Uri="http://camera.local/snapshot")
            )
            mock_media.return_value = mock_service

            mock_aioresponse.get(
                "http://camera.local/snapshot", status=200, body=b"image_data"
            )

            result = await cam.get_snapshot("Profile1")
            assert result == b"image_data"


@pytest.mark.asyncio
async def test_get_snapshot_with_digest_auth_multiple_requests(
    mock_aioresponse: aiointercept,
) -> None:
    """Test that digest auth works correctly across multiple requests."""
    async with create_test_camera() as cam:
        with (
            patch.object(cam, "create_devicemgmt_service", new_callable=AsyncMock),
            patch.object(
                cam, "create_media_service", new_callable=AsyncMock
            ) as mock_media,
        ):
            mock_service = Mock()
            mock_service.create_type = Mock(return_value=Mock())
            mock_service.GetSnapshotUri = AsyncMock(
                return_value=Mock(Uri="http://camera.local/snapshot")
            )
            mock_media.return_value = mock_service

            # Mock multiple successful responses
            mock_aioresponse.get(
                "http://camera.local/snapshot", status=200, body=b"image1"
            )
            mock_aioresponse.get(
                "http://camera.local/snapshot", status=200, body=b"image2"
            )

            # Get snapshots with digest auth
            result1 = await cam.get_snapshot("Profile1", basic_auth=False)
            result2 = await cam.get_snapshot("Profile1", basic_auth=False)

            assert result1 == b"image1"
            assert result2 == b"image2"
            # Check that 2 requests were made (grouped by URL in aiointercept)
            request_list = next(iter(mock_aioresponse.requests.values()))
            assert len(request_list) == 2


@pytest.mark.asyncio
async def test_get_snapshot_mixed_auth_methods(mock_aioresponse: aiointercept) -> None:
    """Test switching between basic and digest auth."""
    async with create_test_camera() as cam:
        with (
            patch.object(cam, "create_devicemgmt_service", new_callable=AsyncMock),
            patch.object(
                cam, "create_media_service", new_callable=AsyncMock
            ) as mock_media,
        ):
            mock_service = Mock()
            mock_service.create_type = Mock(return_value=Mock())
            mock_service.GetSnapshotUri = AsyncMock(
                return_value=Mock(Uri="http://camera.local/snapshot")
            )
            mock_media.return_value = mock_service

            # Mock responses
            mock_aioresponse.get(
                "http://camera.local/snapshot", status=200, body=b"basic_auth_image"
            )
            mock_aioresponse.get(
                "http://camera.local/snapshot", status=200, body=b"digest_auth_image"
            )

            # Test with basic auth
            result1 = await cam.get_snapshot("Profile1", basic_auth=True)
            assert result1 == b"basic_auth_image"

            # Test with digest auth
            result2 = await cam.get_snapshot("Profile1", basic_auth=False)
            assert result2 == b"digest_auth_image"
