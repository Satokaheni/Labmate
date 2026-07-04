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


async def test_storage_manager_opens_without_mongo(monkeypatch, tmp_path):
    """StorageManager() opens no Mongo — it's a pure LocalStore facade."""
    from services.orchestrator import storage_manager as sm_mod
    from services.orchestrator.local_store import LocalStore

    monkeypatch.setattr(sm_mod, "get_local_store", lambda: LocalStore(tmp_path / "state.db"))
    async with sm_mod.StorageManager() as sm:
        assert sm.local_store is not None
        assert sm.context_manager is not None  # constructs without mongo_db


async def test_full_context_manager_usage_works():
    """async with StorageManager() should work without crashing."""
    from services.orchestrator.storage_manager import StorageManager

    storage = StorageManager()

    async with storage as sm:
        assert sm is storage


async def test_storage_manager_constructs_without_args():
    """StorageManager() takes nothing Mongo-related."""
    from services.orchestrator.storage_manager import StorageManager

    storage = StorageManager()

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


def test_context_manager_uses_storage_local_store(storage):
    """ContextManager is wired to the StorageManager's LocalStore, with no Mongo."""

    cm = storage.context_manager
    assert cm.local_store is storage.local_store
    assert cm.chroma == {}  # empty; RAG skipped, compaction still works
    assert not hasattr(cm, "db")
