from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
async def _close_local_stores():
    """Close every LocalStore aiosqlite connection after each memory test.

    The ContextManager tests seed a real LocalStore; an aiosqlite connection left
    open past its per-test event loop raises "RuntimeError: Event loop is closed"
    from its worker thread at teardown (a PytestUnhandledThreadExceptionWarning).
    Draining live stores in-loop here prevents the dangling thread. Mirrors the
    fixture in tests/services/orchestrator/conftest.py.
    """
    yield
    import services.orchestrator.local_store as _ls

    await _ls.close_all_local_stores()
    _ls._STORE = None
