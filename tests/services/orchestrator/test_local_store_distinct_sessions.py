import pytest

from services.orchestrator.local_store import LocalStore


@pytest.mark.asyncio
async def test_distinct_session_ids_recent_first(tmp_path):
    store = LocalStore(tmp_path / "state.db")
    # s1 older, s2 newer; s1 has two turns (distinct must dedupe)
    await store.append_turn("s1", "user", "a", created_at="2026-07-01T00:00:00Z")
    await store.append_turn("s1", "assistant", "b", created_at="2026-07-01T00:00:01Z")
    await store.append_turn("s2", "user", "c", created_at="2026-07-02T00:00:00Z")
    ids = await store.distinct_session_ids()
    assert ids == ["s2", "s1"]  # deduped, most-recent-activity first
    await store.close()


@pytest.mark.asyncio
async def test_distinct_session_ids_empty_and_blank(tmp_path):
    store = LocalStore(tmp_path / "state.db")
    assert await store.distinct_session_ids() == []
    await store.append_turn("", "user", "x")  # blank session id excluded
    assert await store.distinct_session_ids() == []
    await store.close()
