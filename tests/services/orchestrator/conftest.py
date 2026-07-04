from __future__ import annotations

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "mocked: no GPU required, all external calls mocked")
    config.addinivalue_line("markers", "live: requires running llama.cpp inference server")


@pytest.fixture(autouse=True)
async def _close_local_stores():
    """Close every LocalStore aiosqlite connection after each test.

    Each pytest test gets its own event loop; an aiosqlite connection left open
    past its loop raises ``RuntimeError: Event loop is closed`` from its worker
    thread at a later teardown (surfaced as a PytestUnhandledThreadExceptionWarning).
    Draining all live stores in-loop here — plus resetting the process-cached
    singleton — prevents the dangling thread regardless of how a test created its
    store.
    """
    yield
    import services.orchestrator.local_store as _ls

    await _ls.close_all_local_stores()
    _ls._STORE = None


@pytest.fixture
def storage():
    """A plain StorageManager (pure LocalStore facade, no Mongo)."""
    from services.orchestrator.storage_manager import StorageManager

    return StorageManager()
