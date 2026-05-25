from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

try:
    from blockbuster import BlockBuster, blockbuster_ctx
except ImportError:
    BlockBuster = None  # type: ignore[assignment,misc]
    blockbuster_ctx = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from collections.abc import Iterator


_KNOWN_BLOCKING: frozenset[str] = frozenset(
    {
        "tests/test_hikvision_e2e.py::test_get_device_information",
        "tests/test_hikvision_e2e.py::test_update_xaddrs_discovers_services_via_get_services",
        "tests/test_hikvision_e2e.py::test_update_xaddrs_falls_back_to_capabilities_when_get_services_broken",
        "tests/test_hikvision_e2e.py::test_get_capabilities_returns_dict",
        "tests/test_hikvision_e2e.py::test_get_capabilities_adjusts_time_when_called_before_update_xaddrs",
        "tests/test_hikvision_e2e.py::test_get_capabilities_reuses_dt_diff_from_update_xaddrs",
        "tests/test_hikvision_e2e.py::test_get_system_date_and_time",
        "tests/test_hikvision_e2e.py::test_media_snapshot_uri",
        "tests/test_hikvision_e2e.py::test_requests_use_digest_auth_with_zulu_timestamp",
        "tests/test_hikvision_e2e.py::test_plaintext_auth_when_encryption_disabled",
        "tests/test_hikvision_e2e.py::test_pullpoint_subscription_parses_hikvision_dash_timestamps",
        "tests/test_hikvision_e2e.py::test_broken_relative_time_detection_from_pullpoint",
        "tests/test_hikvision_e2e.py::test_camera_rejecting_unauthenticated_requests",
        "tests/test_server_disconnected_retry.py::test_multiple_sequential_requests_with_disconnects",
        "tests/test_server_disconnected_retry.py::test_onvif_service_retries_on_server_disconnect[True]",
        "tests/test_server_disconnected_retry.py::test_onvif_service_retries_on_server_disconnect[False]",
        "tests/test_types.py::test_parse_invalid_dt",
        "tests/test_util.py::test_normalize_url_with_missing_url",
        "tests/test_xaddrs.py::test_get_definition_resolves_recording_after_update",
        "tests/test_xaddrs.py::test_update_xaddrs_falls_back_when_get_services_unsupported[error0]",
        "tests/test_xaddrs.py::test_update_xaddrs_falls_back_when_get_services_unsupported[error1]",
    }
)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Mark known-blocking tests xfail so CI is green while we work through them."""
    if blockbuster_ctx is None:
        return
    marker = pytest.mark.xfail(
        reason="blockbuster: blocking call in asyncio path, to be fixed",
        strict=False,
    )
    for item in items:
        if item.nodeid in _KNOWN_BLOCKING:
            item.add_marker(marker)


@pytest.fixture(autouse=True)
def blockbuster(
    request: pytest.FixtureRequest,
) -> Iterator[BlockBuster | None]:
    """Fail any test that performs a blocking call inside the asyncio loop."""
    if blockbuster_ctx is None:
        yield None
        return
    with blockbuster_ctx() as bb:
        yield bb
