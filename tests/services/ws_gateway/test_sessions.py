import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.ws_gateway.sessions import InMemorySessionStore, build_sessions_router


@pytest.fixture
def store():
    s = InMemorySessionStore()
    s.create(title="Older", mode="chat", session_id="a", updated_at="2026-06-01T00:00:00Z")
    s.create(title="Newer", mode="code", session_id="b", updated_at="2026-06-05T00:00:00Z")
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


def test_get_turns_returns_list(client, store):
    store.add_turn("a", {"id": "t1", "sessionId": "a", "role": "user", "text": "hi", "createdAt": "2026-06-01T00:00:01Z"})
    r = client.get("/sessions/a/turns")
    assert r.status_code == 200
    assert r.json()[0]["text"] == "hi"


def test_patch_unknown_session_404(client):
    r = client.patch("/sessions/zzz", json={"title": "x"})
    assert r.status_code == 404
