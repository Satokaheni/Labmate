import json
import pytest
from fastapi.testclient import TestClient

from services.ws_gateway.config import Config
from services.ws_gateway.server import build_app


@pytest.fixture
def cfg():
    return Config(
        redis_url="redis://localhost:6379/0",
        jwt_secret="test-secret",
        admin_email="admin@labmate.local",
        admin_password="correct-horse",
        jwt_expiry_seconds=3600,
        cors_origins=("http://localhost:5173",),
        mongo_url="mongodb://localhost:27017",
    )


@pytest.fixture
async def app(cfg, redis, seeded_store):
    # Inject the fake redis and all-ready boot checks for deterministic tests.
    async def ready(**_):
        return ("ready", "ok", "")

    checks = {k: ready for k in ("brain", "nervous_system", "hands", "memory", "workspace")}
    return build_app(cfg, redis=redis, boot_checks=checks, user_store=seeded_store)


@pytest.fixture
def client(app):
    return TestClient(app)


def test_unauthenticated_message_before_auth_closes(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "send", "sessionId": "s1", "mode": "chat", "text": "hi"})
        msg = ws.receive_json()
        assert msg["type"] == "auth.error"
        assert msg["reason"] == "invalid"


def test_valid_auth_then_boot_plan_and_ready(client, app):
    token = app.state.auth.mint_token({"id": "u-001", "email": "admin@labmate.local", "role": "admin"})
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": token})
        assert ws.receive_json()["type"] == "auth.ok"
        # boot sequence begins immediately after auth.ok
        plan = ws.receive_json()
        assert plan["type"] == "boot.plan"
        assert len(plan["subsystems"]) == 5
        # drain boot.update frames until boot.ready
        ev = ws.receive_json()
        while ev["type"] == "boot.update":
            ev = ws.receive_json()
        assert ev["type"] == "boot.ready"


def test_bad_token_returns_auth_error(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": "garbage"})
        msg = ws.receive_json()
        assert msg["type"] == "auth.error"
        assert msg["reason"] == "invalid"


def test_send_pushes_task_and_relays_events(client, app, redis):
    token = app.state.auth.mint_token({"id": "u-001", "email": "admin@labmate.local", "role": "admin"})
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": token})
        assert ws.receive_json()["type"] == "auth.ok"
        # drain boot frames
        ev = ws.receive_json()
        while ev["type"] != "boot.ready":
            ev = ws.receive_json()

        ws.send_json({"type": "send", "sessionId": "s1", "mode": "chat", "text": "do it"})
        # server replies with turn.created carrying the new turnId
        created = ws.receive_json()
        assert created["type"] == "turn.created"
        turn_id = created["turn"]["id"]

        # a task must have been pushed to labmate:goals
        import asyncio
        entries = asyncio.get_event_loop().run_until_complete(redis.xrange("labmate:goals"))
        assert len(entries) == 1
        payload = json.loads(entries[0][1]["payload"])
        task_id = payload["task_id"]

        # simulate the orchestrator publishing events for that task
        async def seed():
            stream = f"labmate:events:{task_id}"
            await redis.xadd(stream, {"event": json.dumps({"type": "answer.delta", "task_id": task_id, "seq": 1, "text": "ok"})})
            await redis.xadd(stream, {"event": json.dumps({"type": "turn.done", "task_id": task_id, "seq": 2, "status": "complete"})})
        asyncio.get_event_loop().run_until_complete(seed())

        delta = ws.receive_json()
        assert delta == {"type": "answer.delta", "turnId": turn_id, "text": "ok"}
        done = ws.receive_json()
        assert done == {"type": "turn.done", "turnId": turn_id, "status": "complete"}
