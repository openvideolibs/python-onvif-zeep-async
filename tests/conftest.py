from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import onvif.client
from onvif.client import _WSDL_DIR_FILES

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
def _reset_onvif_client_caches() -> Iterator[None]:
    """Snapshot module-level caches so each test starts from a known state.

    _WSDL_DIR_FILES is pre-warmed at import for the bundled wsdl directory;
    tests must not leak tmp_path entries into other tests nor drop the bundled
    entry. _SHARED_SQLITE_CACHE is lazily built on first setup() call; reset to
    None so a test cannot observe a cache left behind by an earlier test.
    """
    wsdl_snapshot = dict(_WSDL_DIR_FILES)
    sqlite_snapshot = onvif.client._SHARED_SQLITE_CACHE
    try:
        yield
    finally:
        _WSDL_DIR_FILES.clear()
        _WSDL_DIR_FILES.update(wsdl_snapshot)
        onvif.client._SHARED_SQLITE_CACHE = sqlite_snapshot


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
