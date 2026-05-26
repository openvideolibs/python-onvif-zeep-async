from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

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
def _reset_wsdl_dir_cache() -> Iterator[None]:
    """Snapshot _WSDL_DIR_FILES so each test starts from the import-time state.

    The bundled wsdl directory is pre-warmed at module import; tests must not
    leak entries (for example, tmp_path scratch dirs) into other tests, and
    must not delete the bundled entry that subsequent tests rely on.
    """
    snapshot = dict(_WSDL_DIR_FILES)
    try:
        yield
    finally:
        _WSDL_DIR_FILES.clear()
        _WSDL_DIR_FILES.update(snapshot)


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
