import pytest

from services.orchestrator.local_store import LocalStore


@pytest.mark.asyncio
async def test_payload_round_trip_and_plain_null(tmp_path):
    store = LocalStore(tmp_path / "state.db")
    rich = {
        "id": "t-1",
        "role": "assistant",
        "text": "hi",
        "reasoning": {"text": "r"},
        "toolCalls": [{"name": "x"}],
    }
    seq = await store.append_turn_payload("s1", "assistant", "hi", rich)
    assert seq == 0
    await store.append_turn("s1", "user", "plain")  # legacy writer → payload NULL
    rows = await store.turns_with_payload("s1")
    assert rows[0]["payload"] == rich and rows[0]["role"] == "assistant"
    assert rows[1]["payload"] is None and rows[1]["text"] == "plain"
    # legacy readers unaffected
    assert [t["text"] for t in await store.all_turns("s1")] == ["hi", "plain"]
    await store.close()


@pytest.mark.asyncio
async def test_delete_session_removes_turns_and_kv(tmp_path):
    store = LocalStore(tmp_path / "state.db")
    await store.record_session("s1", user_id="u")
    await store.append_turn("s1", "user", "hi")
    await store.session_kv_set("gw", "s1", '{"title":"T"}')
    await store.delete_session("s1")
    assert await store.all_turns("s1") == []
    assert await store.session_kv_get("gw", "s1") is None
    assert await store.list_sessions("u") == []
    await store.close()
