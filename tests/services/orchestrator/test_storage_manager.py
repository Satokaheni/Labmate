from __future__ import annotations

import pytest

pytestmark = [pytest.mark.mocked, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def _reset_local_store():
    """Isolate the process-cached LocalStore singleton (services.orchestrator.local_store._STORE).

    Several tests in this file touch StorageManager.local_store (directly or via the
    context_manager property), which lazily populates the module-global singleton.
    Reset it around every test so this file never leaks a cached store into — or
    picks one up from — other test modules (e.g. test_local_store.py's singleton test).
    """
    import services.orchestrator.local_store as ls

    ls._STORE = None
    yield
    ls._STORE = None


# ---------------------------------------------------------------------------
# StorageManager async context manager
# ---------------------------------------------------------------------------


async def test_context_manager_closes_connections_on_exit(mock_mongo):
    """__aexit__ must close the Mongo connection."""
    from unittest.mock import MagicMock

    from services.orchestrator.storage_manager import StorageManager

    mock_mongo_with_close = MagicMock()
    mock_mongo_with_close.close = MagicMock()

    storage = StorageManager.from_clients(mongo=mock_mongo_with_close)

    await storage.__aexit__(None, None, None)

    # Mongo should be closed
    mock_mongo_with_close.close.assert_called_once()


async def test_full_context_manager_usage_works(mock_mongo):
    """async with StorageManager() should work without crashing."""
    from services.orchestrator.storage_manager import StorageManager

    storage = StorageManager.from_clients(mongo=mock_mongo)

    async with storage as sm:
        assert sm is storage


async def test_from_clients_works_without_context_manager():
    """from_clients path should not require async context manager entry."""
    from unittest.mock import MagicMock

    from services.orchestrator.storage_manager import StorageManager

    mock_mongo = MagicMock()

    # This should work without entering the context manager
    storage = StorageManager.from_clients(mongo=mock_mongo)

    # Should be able to call methods directly
    assert storage is not None


@pytest.mark.asyncio
async def test_storage_manager_has_workspace_manager(storage):
    """StorageManager exposes a WorkspaceManager via .workspaces property."""
    from services.orchestrator.workspace_manager import WorkspaceManager

    assert isinstance(storage.workspaces, WorkspaceManager)


@pytest.mark.asyncio
async def test_storage_manager_workspace_manager_uses_same_local_store(
    storage, tmp_path, monkeypatch
):
    """WorkspaceManager shares the same LocalStore instance as StorageManager.local_store."""
    monkeypatch.setenv("LABMATE_STATE_DB", str(tmp_path / "s.sqlite"))
    assert storage.workspaces._store is storage.local_store


# ---------------------------------------------------------------------------
# context_manager property
# ---------------------------------------------------------------------------


def test_context_manager_property_returns_context_manager_instance(storage):
    """StorageManager.context_manager returns a ContextManager and is lazily cached."""
    from services.memory.context_manager import ContextManager

    cm = storage.context_manager
    assert isinstance(cm, ContextManager)
    # Lazily cached — same object on repeated access
    assert storage.context_manager is cm


def test_context_manager_uses_storage_local_store_and_db(storage):
    """ContextManager is wired to the StorageManager's LocalStore and MongoDB."""

    cm = storage.context_manager
    assert cm.local_store is storage.local_store
    assert cm.db is storage._db
    assert cm.chroma == {}  # empty; RAG skipped, compaction still works
