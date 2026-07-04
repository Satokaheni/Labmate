import pytest

from services.orchestrator.inproc_bus import SignalRegistry
from services.orchestrator.local_store import LocalStore
from services.orchestrator.main import OrchestratorProcess


@pytest.mark.asyncio
async def test_persist_turns_skips_when_relay_owned(tmp_path, monkeypatch):
    proc = OrchestratorProcess()
    proc.signals = SignalRegistry()
    store = LocalStore(tmp_path / "state.db")

    class _Storage:
        local_store = store

    # relay owns persistence for this session → orchestrator must NOT write
    proc.signals.mark_persistence_owned("s-owned")
    await proc._persist_turns(_Storage(), "s-owned", "u", "a")
    assert await store.all_turns("s-owned") == []

    # not owned → orchestrator writes the fallback plain turns
    await proc._persist_turns(_Storage(), "s-free", "u", "a")
    assert [t["role"] for t in await store.all_turns("s-free")] == ["user", "assistant"]
    await store.close()
