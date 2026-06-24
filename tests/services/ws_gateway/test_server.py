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


def test_tool_result_message_writes_to_redis(client, app, redis):
    """A tool.result message from the client is written to labmate:tool-results:<task_id>."""
    import asyncio
    token = app.state.auth.mint_token({"id": "u-001", "email": "admin@labmate.local", "role": "admin"})
    with client.websocket_connect("/ws") as ws:
        # auth + boot
        ws.send_json({"type": "auth", "token": token})
        assert ws.receive_json()["type"] == "auth.ok"
        ev = ws.receive_json()
        while ev["type"] != "boot.ready":
            ev = ws.receive_json()

        # send a task to establish an active_task_id
        ws.send_json({"type": "send", "sessionId": "s1", "mode": "chat", "text": "read a file"})
        created = ws.receive_json()
        assert created["type"] == "turn.created"

        # get the task_id from the goals stream
        entries = asyncio.get_event_loop().run_until_complete(redis.xrange("labmate:goals"))
        payload = json.loads(entries[0][1]["payload"])
        task_id = payload["task_id"]

        # send a tool.result (simulating Electron completing a local tool)
        ws.send_json({
            "type": "tool.result",
            "toolRequestId": "req-42",
            "result": {"content": "file contents"},
            "error": None,
        })

        # yield control so the server can process the tool.result
        asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.05))

        # verify the frame landed in the tool-results stream
        result_entries = asyncio.get_event_loop().run_until_complete(
            redis.xrange(f"labmate:tool-results:{task_id}")
        )
        assert len(result_entries) == 1
        frame = json.loads(result_entries[0][1]["result"])
        assert frame["tool_request_id"] == "req-42"
        assert frame["result"] == {"content": "file contents"}
        assert frame["error"] is None


def _boot_to_ready(ws, app):
    """Authenticate and drain all boot frames. Returns the boot.ready event."""
    token = app.state.auth.mint_token({"id": "u-001", "email": "admin@labmate.local", "role": "admin"})
    ws.send_json({"type": "auth", "token": token})
    assert ws.receive_json()["type"] == "auth.ok"
    ev = ws.receive_json()
    while ev["type"] != "boot.ready":
        ev = ws.receive_json()
    return ev


def test_session_new_creates_session_and_emits_session_updated(client, app):
    with client.websocket_connect("/ws") as ws:
        _boot_to_ready(ws, app)
        ws.send_json({"type": "session.new", "mode": "code"})
        msg = ws.receive_json()
        assert msg["type"] == "session.updated"
        assert msg["session"]["mode"] == "code"
        assert "id" in msg["session"]


def test_session_rename_emits_session_updated(client, app):
    with client.websocket_connect("/ws") as ws:
        _boot_to_ready(ws, app)
        ws.send_json({"type": "session.new", "mode": "chat"})
        created = ws.receive_json()
        sid = created["session"]["id"]

        ws.send_json({"type": "session.rename", "sessionId": sid, "title": "My Chat"})
        renamed = ws.receive_json()
        assert renamed["type"] == "session.updated"
        assert renamed["session"]["title"] == "My Chat"


def test_session_open_emits_session_updated(client, app):
    with client.websocket_connect("/ws") as ws:
        _boot_to_ready(ws, app)
        ws.send_json({"type": "session.new", "mode": "paper"})
        created = ws.receive_json()
        sid = created["session"]["id"]

        ws.send_json({"type": "session.open", "sessionId": sid})
        opened = ws.receive_json()
        assert opened["type"] == "session.updated"
        assert opened["session"]["id"] == sid


def test_relay_emits_reasoning_done_before_turn_done(client, app, redis):
    """_relay_task must synthesize reasoning.done from accumulated reasoning events."""
    import asyncio
    import json as _json

    token = app.state.auth.mint_token({"id": "u-001", "email": "admin@labmate.local", "role": "admin"})
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": token})
        assert ws.receive_json()["type"] == "auth.ok"
        ev = ws.receive_json()
        while ev["type"] != "boot.ready":
            ev = ws.receive_json()

        ws.send_json({"type": "send", "sessionId": "s1", "mode": "chat", "text": "think hard"})
        ev = ws.receive_json()
        while ev["type"] != "turn.created":
            ev = ws.receive_json()

        # Find task_id from goals stream
        entries = asyncio.get_event_loop().run_until_complete(redis.xrange("labmate:goals"))
        task_id = _json.loads(entries[-1][1]["payload"])["task_id"]

        # Inject reasoning events + turn.done into the events stream
        stream = f"labmate:events:{task_id}"
        asyncio.get_event_loop().run_until_complete(redis.xadd(stream, {"event": _json.dumps(
            {"type": "reasoning", "task_id": task_id, "seq": 1, "node": "plan_node", "text": "I think"}
        )}))
        asyncio.get_event_loop().run_until_complete(redis.xadd(stream, {"event": _json.dumps(
            {"type": "reasoning", "task_id": task_id, "seq": 2, "node": "plan_node", "text": " carefully"}
        )}))
        asyncio.get_event_loop().run_until_complete(redis.xadd(stream, {"event": _json.dumps(
            {"type": "turn.done", "task_id": task_id, "seq": 3, "status": "complete"}
        )}))

        # Collect events until turn.done
        received = []
        ev = ws.receive_json()
        while True:
            received.append(ev)
            if ev["type"] == "turn.done":
                break
            ev = ws.receive_json()

        types = [e["type"] for e in received]
        assert "reasoning.delta" in types
        assert "reasoning.done" in types

        # reasoning.done must appear before turn.done
        rdone_idx = next(i for i, e in enumerate(received) if e["type"] == "reasoning.done")
        tdone_idx = next(i for i, e in enumerate(received) if e["type"] == "turn.done")
        assert rdone_idx < tdone_idx

        rdone = next(e for e in received if e["type"] == "reasoning.done")
        assert rdone["reasoning"]["text"] == "I think carefully"
        assert rdone["reasoning"]["summary"] == "I think carefully"
        assert rdone["reasoning"]["node"] == "plan_node"
