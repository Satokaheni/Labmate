import pytest

from services.orchestrator.local_store import LocalStore


@pytest.mark.asyncio
async def test_checkpoint_put_get_delete_upsert(tmp_path):
    store = LocalStore(tmp_path / "state.db")
    assert await store.checkpoint_get("t1") is None
    await store.checkpoint_put("t1", {"turn": 1, "goal": "g"})
    assert await store.checkpoint_get("t1") == {"turn": 1, "goal": "g"}
    await store.checkpoint_put("t1", {"turn": 2, "goal": "g"})  # upsert
    assert (await store.checkpoint_get("t1"))["turn"] == 2
    await store.checkpoint_delete("t1")
    assert await store.checkpoint_get("t1") is None
    await store.close()
