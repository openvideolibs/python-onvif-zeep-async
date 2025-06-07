"""Tests for snapshot functionality using aiohttp."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock, patch

import pytest_asyncio

import aiohttp
import pytest
from aioresponses import aioresponses
from onvif import ONVIFCamera
from onvif.exceptions import ONVIFAuthError, ONVIFError, ONVIFTimeoutError


@pytest.fixture
def mock_aioresponse():
    """Return aioresponses fixture."""
    # Note: aioresponses will mock all ClientSession instances by default
    with aioresponses(passthrough=["http://127.0.0.1:8123"]) as m:
        yield m


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
                return_value=Mock(Uri="http://192.168.1.100/snapshot")
            )
            mock_media.return_value = mock_service
            yield cam


@pytest.mark.asyncio
async def test_get_snapshot_success_with_digest_auth(
    camera: ONVIFCamera, mock_aioresponse: aioresponses
) -> None:
    """Test successful snapshot retrieval with digest authentication."""
    snapshot_data = b"fake_image_data"

    # Mock successful response
    mock_aioresponse.get(
        "http://192.168.1.100/snapshot", status=200, body=snapshot_data
    )

    # Get snapshot with digest auth (default)
    result = await camera.get_snapshot("Profile1", basic_auth=False)

    assert result == snapshot_data

    # Check that the request was made
    assert len(mock_aioresponse.requests) == 1
    request_key = next(iter(mock_aioresponse.requests.keys()))
    assert str(request_key[1]).startswith("http://192.168.1.100/snapshot")


@pytest.mark.asyncio
async def test_get_snapshot_success_with_basic_auth(
    camera: ONVIFCamera, mock_aioresponse: aioresponses
) -> None:
    """Test successful snapshot retrieval with basic authentication."""
    snapshot_data = b"fake_image_data"

    # Mock successful response
    mock_aioresponse.get(
        "http://192.168.1.100/snapshot", status=200, body=snapshot_data
    )

    # Get snapshot with basic auth
    result = await camera.get_snapshot("Profile1", basic_auth=True)

    assert result == snapshot_data

    # Check that the request was made
    assert len(mock_aioresponse.requests) == 1
    request_key = next(iter(mock_aioresponse.requests.keys()))
    assert str(request_key[1]).startswith("http://192.168.1.100/snapshot")


@pytest.mark.asyncio
async def test_get_snapshot_auth_failure(
    camera: ONVIFCamera, mock_aioresponse: aioresponses
) -> None:
    """Test snapshot retrieval with authentication failure."""
    # Mock 401 response
    mock_aioresponse.get(
        "http://192.168.1.100/snapshot", status=401, body=b"Unauthorized"
    )

    # Should raise ONVIFAuthError
    with pytest.raises(ONVIFAuthError) as exc_info:
        await camera.get_snapshot("Profile1")

    assert "Failed to authenticate" in str(exc_info.value)


@pytest.mark.asyncio
async def test_get_snapshot_with_user_pass_in_url(
    camera: ONVIFCamera, mock_aioresponse: aioresponses
) -> None:
    """Test snapshot retrieval when URI contains credentials."""
    # Mock the media service to return URI with credentials
    with patch.object(
        camera, "create_media_service", new_callable=AsyncMock
    ) as mock_media:
        mock_service = Mock()
        mock_service.create_type = Mock(return_value=Mock())
        mock_service.GetSnapshotUri = AsyncMock(
            return_value=Mock(Uri="http://admin:password@192.168.1.100/snapshot")
        )
        mock_media.return_value = mock_service

        # First request fails with 401
        mock_aioresponse.get(
            "http://admin:password@192.168.1.100/snapshot",
            status=401,
            body=b"Unauthorized",
        )
        # Second request succeeds (stripped URL)
        mock_aioresponse.get(
            "http://192.168.1.100/snapshot", status=200, body=b"image_data"
        )

        result = await camera.get_snapshot("Profile1")

        assert result == b"image_data"
        # Should have made 2 requests - first with credentials in URL, second without
        request_keys = list(mock_aioresponse.requests.keys())
        assert len(request_keys) == 2
        assert str(request_keys[0][1]) == "http://admin:password@192.168.1.100/snapshot"
        assert str(request_keys[1][1]) == "http://192.168.1.100/snapshot"


@pytest.mark.asyncio
async def test_get_snapshot_timeout(
    camera: ONVIFCamera, mock_aioresponse: aioresponses
) -> None:
    """Test snapshot retrieval timeout."""
    # Mock timeout by raising TimeoutError
    mock_aioresponse.get(
        "http://192.168.1.100/snapshot", exception=TimeoutError("Connection timeout")
    )

    with pytest.raises(ONVIFTimeoutError) as exc_info:
        await camera.get_snapshot("Profile1")

    assert "Timed out fetching" in str(exc_info.value)


@pytest.mark.asyncio
async def test_get_snapshot_client_error(
    camera: ONVIFCamera, mock_aioresponse: aioresponses
) -> None:
    """Test snapshot retrieval with client error."""
    # Mock client error
    mock_aioresponse.get(
        "http://192.168.1.100/snapshot",
        exception=aiohttp.ClientError("Connection failed"),
    )

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

        import zeep.exceptions

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
    camera: ONVIFCamera, mock_aioresponse: aioresponses
) -> None:
    """Test snapshot retrieval with 404 error."""
    # Mock 404 response
    mock_aioresponse.get("http://192.168.1.100/snapshot", status=404, body=b"Not Found")

    result = await camera.get_snapshot("Profile1")

    # Should return None for non-auth errors
    assert result is None


@pytest.mark.asyncio
async def test_get_snapshot_uri_caching(camera: ONVIFCamera) -> None:
    """Test that snapshot URI is cached after first retrieval."""
    # First call should fetch URI from service
    uri = await camera.get_snapshot_uri("Profile1")
    assert uri == "http://192.168.1.100/snapshot"

    # Mock the media service to ensure it's not called again
    with patch.object(
        camera, "create_media_service", new_callable=AsyncMock
    ) as mock_media:
        mock_media.side_effect = Exception("Should not be called")

        # Second call should use cached URI
        uri2 = await camera.get_snapshot_uri("Profile1")
        assert uri2 == "http://192.168.1.100/snapshot"

        # Mock media service should not have been called
        mock_media.assert_not_called()


@pytest.mark.asyncio
async def test_snapshot_client_session_reuse(
    camera: ONVIFCamera, mock_aioresponse: aioresponses
) -> None:
    """Test that snapshot client session is reused across requests."""
    snapshot_data = b"fake_image_data"

    # Get reference to the snapshot client
    snapshot_client = camera._snapshot_client

    # Mock multiple requests
    mock_aioresponse.get(
        "http://192.168.1.100/snapshot", status=200, body=snapshot_data
    )
    mock_aioresponse.get(
        "http://192.168.1.100/snapshot", status=200, body=snapshot_data
    )

    # Make multiple snapshot requests
    result1 = await camera.get_snapshot("Profile1")
    result2 = await camera.get_snapshot("Profile1")

    assert result1 == snapshot_data
    assert result2 == snapshot_data

    # Verify same client session was used
    assert camera._snapshot_client is snapshot_client


@pytest.mark.asyncio
async def test_get_snapshot_no_credentials(mock_aioresponse: aioresponses) -> None:
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
                return_value=Mock(Uri="http://192.168.1.100/snapshot")
            )
            mock_media.return_value = mock_service

            mock_aioresponse.get(
                "http://192.168.1.100/snapshot", status=200, body=b"image_data"
            )

            result = await cam.get_snapshot("Profile1")
            assert result == b"image_data"


@pytest.mark.asyncio
async def test_get_snapshot_with_digest_auth_multiple_requests(
    mock_aioresponse: aioresponses,
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
                return_value=Mock(Uri="http://192.168.1.100/snapshot")
            )
            mock_media.return_value = mock_service

            # Mock multiple successful responses
            mock_aioresponse.get(
                "http://192.168.1.100/snapshot", status=200, body=b"image1"
            )
            mock_aioresponse.get(
                "http://192.168.1.100/snapshot", status=200, body=b"image2"
            )

            # Get snapshots with digest auth
            result1 = await cam.get_snapshot("Profile1", basic_auth=False)
            result2 = await cam.get_snapshot("Profile1", basic_auth=False)

            assert result1 == b"image1"
            assert result2 == b"image2"
            # Check that 2 requests were made (they're grouped by URL in aioresponses)
            request_list = next(iter(mock_aioresponse.requests.values()))
            assert len(request_list) == 2


@pytest.mark.asyncio
async def test_get_snapshot_mixed_auth_methods(mock_aioresponse: aioresponses) -> None:
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
                return_value=Mock(Uri="http://192.168.1.100/snapshot")
            )
            mock_media.return_value = mock_service

            # Mock responses
            mock_aioresponse.get(
                "http://192.168.1.100/snapshot", status=200, body=b"basic_auth_image"
            )
            mock_aioresponse.get(
                "http://192.168.1.100/snapshot", status=200, body=b"digest_auth_image"
            )

            # Test with basic auth
            result1 = await cam.get_snapshot("Profile1", basic_auth=True)
            assert result1 == b"basic_auth_image"

            # Test with digest auth
            result2 = await cam.get_snapshot("Profile1", basic_auth=False)
            assert result2 == b"digest_auth_image"
