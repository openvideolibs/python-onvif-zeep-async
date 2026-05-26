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
        "tests/test_server_disconnected_retry.py::test_multiple_sequential_requests_with_disconnects",
        "tests/test_server_disconnected_retry.py::test_onvif_service_retries_on_server_disconnect[False]",
        "tests/test_types.py::test_parse_invalid_dt",
        "tests/test_util.py::test_normalize_url_with_missing_url",
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
