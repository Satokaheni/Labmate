import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.ws_gateway.sessions import InMemorySessionStore, build_sessions_router


@pytest.fixture
async def store():
    s = InMemorySessionStore()
    await s.create(title="Older", mode="chat", session_id="a", updated_at="2026-06-01T00:00:00Z")
    await s.create(title="Newer", mode="code", session_id="b", updated_at="2026-06-05T00:00:00Z")
    return s


@pytest.fixture
def client(store):
    app = FastAPI()
    app.include_router(build_sessions_router(store))
    return TestClient(app)


def test_get_sessions_sorted_by_updated_desc(client):
    r = client.get("/sessions")
    assert r.status_code == 200
    titles = [s["title"] for s in r.json()]
    assert titles == ["Newer", "Older"]


def test_post_creates_session(client):
    r = client.post("/sessions", json={"mode": "paper", "title": "Fresh"})
    assert r.status_code == 201
    body = r.json()
    assert body["mode"] == "paper"
    assert body["title"] == "Fresh"
    assert body["id"]


def test_patch_renames_session(client):
    r = client.patch("/sessions/a", json={"title": "Renamed"})
    assert r.status_code == 200
    assert r.json()["title"] == "Renamed"


@pytest.mark.asyncio
async def test_get_turns_returns_list(client, store):
    await store.add_turn(
        "a",
        {
            "id": "t1",
            "sessionId": "a",
            "role": "user",
            "text": "hi",
            "createdAt": "2026-06-01T00:00:01Z",
        },
    )
    r = client.get("/sessions/a/turns")
    assert r.status_code == 200
    assert r.json()[0]["text"] == "hi"


def test_patch_unknown_session_404(client):
    r = client.patch("/sessions/zzz", json={"title": "x"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_set_debug_toggles_flag():
    from services.ws_gateway.sessions import InMemorySessionStore

    store = InMemorySessionStore()
    s = await store.create(title="Debug session", mode="code")
    sid = s["id"]

    assert await store.get_debug(sid) is False
    await store.set_debug(sid, True)
    assert await store.get_debug(sid) is True
    await store.set_debug(sid, False)
    assert await store.get_debug(sid) is False


@pytest.mark.asyncio
async def test_get_debug_unknown_session_returns_false():
    from services.ws_gateway.sessions import InMemorySessionStore

    assert await InMemorySessionStore().get_debug("nonexistent") is False


@pytest.mark.asyncio
async def test_add_turn_sets_monotonic_seq():
    from services.ws_gateway.sessions import InMemorySessionStore

    store = InMemorySessionStore()
    await store.create(title="Test", mode="chat", session_id="s-1")

    # Add first turn
    await store.add_turn("s-1", {"id": "t-1", "sessionId": "s-1", "role": "user", "text": "first"})
    # Add second turn
    await store.add_turn(
        "s-1", {"id": "t-2", "sessionId": "s-1", "role": "assistant", "text": "second"}
    )

    turns = await store.turns("s-1")

    assert len(turns) == 2
    # First turn should have seq == 0
    assert turns[0]["seq"] == 0
    # Second turn should have seq == 1
    assert turns[1]["seq"] == 1


@pytest.mark.asyncio
async def test_turns_seq_independent_per_session():
    from services.ws_gateway.sessions import InMemorySessionStore

    store = InMemorySessionStore()

    # Create two sessions
    await store.create(title="Session 1", mode="chat", session_id="s-1")
    await store.create(title="Session 2", mode="chat", session_id="s-2")

    # Add turn to first session
    await store.add_turn("s-1", {"id": "t-1", "sessionId": "s-1", "role": "user", "text": "s1-t1"})
    await store.add_turn("s-1", {"id": "t-2", "sessionId": "s-1", "role": "user", "text": "s1-t2"})

    # Add turn to second session (should start seq at 0)
    await store.add_turn("s-2", {"id": "t-3", "sessionId": "s-2", "role": "user", "text": "s2-t1"})

    turns_s1 = await store.turns("s-1")
    turns_s2 = await store.turns("s-2")

    assert turns_s1[0]["seq"] == 0
    assert turns_s1[1]["seq"] == 1
    # Second session starts its seq at 0
    assert turns_s2[0]["seq"] == 0
