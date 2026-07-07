import pytest

from services.orchestrator.local_store import LocalStore
from services.ws_gateway.sqlite_session_store import SqliteSessionStore


@pytest.mark.asyncio
async def test_session_lifecycle_and_turn_payload_round_trip(tmp_path):
    ss = SqliteSessionStore(LocalStore(tmp_path / "state.db"))
    s = await ss.create(title="Hello", mode="chat", session_id="s1")
    assert s == {
        "id": "s1",
        "title": "Hello",
        "mode": "chat",
        "turnCount": 0,
        "contextTokens": 0,
        "createdAt": s["createdAt"],
        "updatedAt": s["updatedAt"],
    }
    assert (await ss.get("s1"))["title"] == "Hello"
    assert [x["id"] for x in await ss.list()] == ["s1"]

    turn = {
        "id": "t-1",
        "sessionId": "s1",
        "role": "assistant",
        "text": "hi",
        "reasoning": {"text": "r"},
        "toolCalls": [{"name": "x"}],
        "createdAt": "2026-07-04T00:00:00Z",
        "status": "complete",
    }
    await ss.add_turn("s1", turn)
    got = await ss.turns("s1")
    assert got[0]["reasoning"] == {"text": "r"} and got[0]["toolCalls"] == [{"name": "x"}]
    assert got[0]["id"] == "t-1" and got[0]["seq"] == 0
    assert (await ss.get("s1"))["turnCount"] == 1

    await ss.rename("s1", "Renamed")
    assert (await ss.get("s1"))["title"] == "Renamed"
    await ss.set_debug("s1", True)
    assert await ss.get_debug("s1") is True

    assert await ss.delete("s1") is True
    assert await ss.get("s1") is None
    assert await ss.delete("s1") is False


@pytest.mark.asyncio
async def test_turns_without_payload_reconstructed(tmp_path):
    store = LocalStore(tmp_path / "state.db")
    ss = SqliteSessionStore(store)
    await ss.create(title="T", mode="chat", session_id="s1")
    await store.append_turn("s1", "user", "plain")  # orchestrator fallback writer
    got = await ss.turns("s1")
    assert got[0]["role"] == "user" and got[0]["text"] == "plain"
    assert got[0]["sessionId"] == "s1" and got[0]["seq"] == 0
