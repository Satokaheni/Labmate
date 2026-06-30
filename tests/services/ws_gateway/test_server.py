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
    token = app.state.auth.mint_token(
        {"id": "u-001", "email": "admin@labmate.local", "role": "admin"}
    )
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
    token = app.state.auth.mint_token(
        {"id": "u-001", "email": "admin@labmate.local", "role": "admin"}
    )
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": token})
        assert ws.receive_json()["type"] == "auth.ok"
        # drain boot frames
        ev = ws.receive_json()
        while ev["type"] != "boot.ready":
            ev = ws.receive_json()

        ws.send_json({"type": "send", "sessionId": "s1", "mode": "chat", "text": "do it"})
        # s1 is unknown, so the server auto-creates + titles it first, then emits turns
        created_user = ws.receive_json()
        if created_user["type"] == "session.updated":
            created_user = ws.receive_json()
        assert created_user["type"] == "turn.created"
        assert created_user["turn"]["role"] == "user"
        created_asst = ws.receive_json()
        assert created_asst["type"] == "turn.created"
        assert created_asst["turn"]["role"] == "assistant"
        assistant_turn_id = created_asst["turn"]["id"]

        # a task must have been pushed to labmate:goals
        import asyncio

        entries = asyncio.get_event_loop().run_until_complete(redis.xrange("labmate:goals"))
        assert len(entries) == 1
        payload = json.loads(entries[0][1]["payload"])
        task_id = payload["task_id"]

        # simulate the orchestrator publishing events for that task
        async def seed():
            stream = f"labmate:events:{task_id}"
            await redis.xadd(
                stream,
                {
                    "event": json.dumps(
                        {"type": "answer.delta", "task_id": task_id, "seq": 1, "text": "ok"}
                    )
                },
            )
            await redis.xadd(
                stream,
                {
                    "event": json.dumps(
                        {"type": "turn.done", "task_id": task_id, "seq": 2, "status": "complete"}
                    )
                },
            )

        asyncio.get_event_loop().run_until_complete(seed())

        delta = ws.receive_json()
        assert delta == {"type": "answer.delta", "turnId": assistant_turn_id, "text": "ok"}
        done = ws.receive_json()
        assert done == {"type": "turn.done", "turnId": assistant_turn_id, "status": "complete"}


def test_tool_result_message_writes_to_redis(client, app, redis):
    """A tool.result message from the client is written to labmate:tool-results:<task_id>."""
    import asyncio

    token = app.state.auth.mint_token(
        {"id": "u-001", "email": "admin@labmate.local", "role": "admin"}
    )
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
        if created["type"] == "session.updated":  # auto-created session for s1
            created = ws.receive_json()
        assert created["type"] == "turn.created"

        # get the task_id from the goals stream
        entries = asyncio.get_event_loop().run_until_complete(redis.xrange("labmate:goals"))
        payload = json.loads(entries[0][1]["payload"])
        task_id = payload["task_id"]

        # send a tool.result (simulating Electron completing a local tool)
        ws.send_json(
            {
                "type": "tool.result",
                "toolRequestId": "req-42",
                "result": {"content": "file contents"},
                "error": None,
            }
        )

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


def test_cancel_writes_redis_flag_and_emits_turn_done_error(client, app, redis):
    """cancel must write the Redis cancel key and emit turn.done:error."""
    import asyncio
    import json as _json

    import fakeredis as _fakeredis
    from fakeredis._server import FakeServer

    token = app.state.auth.mint_token(
        {"id": "u-001", "email": "admin@labmate.local", "role": "admin"}
    )
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": token})
        assert ws.receive_json()["type"] == "auth.ok"
        ev = ws.receive_json()
        while ev["type"] != "boot.ready":
            ev = ws.receive_json()

        # Start a task
        ws.send_json({"type": "send", "sessionId": "s1", "mode": "chat", "text": "run"})
        ev = ws.receive_json()
        while ev["type"] != "turn.created":
            ev = ws.receive_json()
        # ev is the user turn; the next turn.created is the assistant turn
        ev_asst = ws.receive_json()
        assert ev_asst["type"] == "turn.created"
        assert ev_asst["turn"]["role"] == "assistant"
        assistant_turn_id = ev_asst["turn"]["id"]

        # Get the task_id from goals stream
        entries = asyncio.get_event_loop().run_until_complete(redis.xrange("labmate:goals"))
        task_id = _json.loads(entries[-1][1]["payload"])["task_id"]

        # Cancel the assistant turn
        ws.send_json({"type": "cancel", "sessionId": "s1", "turnId": assistant_turn_id})
        resp = ws.receive_json()
        assert resp["type"] == "turn.done"
        assert resp["status"] == "error"
        assert resp["turnId"] == assistant_turn_id

    # After the WS context closes, check the Redis cancel flag.
    # We use a synchronous FakeRedis sharing the same FakeServer (located by pool host key)
    # to avoid cross-event-loop issues with the async fakeredis connection's internal locks.
    pool_kwargs = redis.connection_pool.connection_kwargs
    host = pool_kwargs.get("host")
    port = pool_kwargs.get("port", 6379)
    version = pool_kwargs.get("version", (7,))
    server_type = pool_kwargs.get("server_type", "redis")
    v_str = ".".join(str(x) for x in version[:1])
    server_key = f"{host}:{port}:{server_type}:v{v_str}"
    shared_server = FakeServer.get_server(server_key, version=version, server_type=server_type)
    r_sync = _fakeredis.FakeRedis(server=shared_server, decode_responses=True)
    flag = r_sync.exists(f"labmate:cancel:{task_id}")
    assert flag == 1


def _boot_to_ready(ws, app):
    """Authenticate and drain all boot frames. Returns the boot.ready event."""
    token = app.state.auth.mint_token(
        {"id": "u-001", "email": "admin@labmate.local", "role": "admin"}
    )
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


def test_title_from_message():
    from services.ws_gateway.server import _title_from_message

    assert _title_from_message("hello world") == "hello world"
    assert _title_from_message("") == "New session"
    assert _title_from_message("\n\n  first line  \nsecond") == "first line"
    out = _title_from_message("x" * 60)
    assert out.endswith("…") and len(out) <= 49


def test_send_auto_creates_and_titles_session(client, app, redis):
    """A send to an unknown session id auto-creates it, titled from the message."""
    token = app.state.auth.mint_token(
        {"id": "u-001", "email": "admin@labmate.local", "role": "admin"}
    )
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": token})
        assert ws.receive_json()["type"] == "auth.ok"
        ev = ws.receive_json()
        while ev["type"] != "boot.ready":
            ev = ws.receive_json()

        ws.send_json(
            {
                "type": "send",
                "sessionId": "s-fresh",
                "mode": "code",
                "text": "Implement the MCP bridge\nover stdio",
            }
        )
        first = ws.receive_json()
        assert first["type"] == "session.updated"
        assert first["session"]["id"] == "s-fresh"
        assert first["session"]["title"] == "Implement the MCP bridge"
        assert first["session"]["mode"] == "code"


def test_client_capabilities_frame_is_threaded_into_task_payload(client, app, redis):
    """A client.capabilities frame followed by send results in push_task receiving the manifest."""
    import asyncio
    import json as _json

    token = app.state.auth.mint_token(
        {"id": "u-001", "email": "admin@labmate.local", "role": "admin"}
    )
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": token})
        assert ws.receive_json()["type"] == "auth.ok"
        ev = ws.receive_json()
        while ev["type"] != "boot.ready":
            ev = ws.receive_json()

        # Send client capabilities frame
        capabilities = {
            "protocolVersion": 1,
            "tools": [
                {"name": "read_file", "source": "builtin"},
                {"name": "write_file", "source": "builtin"},
            ],
        }
        ws.send_json({"type": "client.capabilities", **capabilities})

        # Now send a task
        ws.send_json({"type": "send", "sessionId": "s1", "mode": "chat", "text": "do it"})
        ev = ws.receive_json()
        while ev["type"] != "turn.created":
            ev = ws.receive_json()

        # Get task_id from goals stream and verify payload includes client_capabilities
        entries = asyncio.get_event_loop().run_until_complete(redis.xrange("labmate:goals"))
        assert len(entries) >= 1
        payload = _json.loads(entries[-1][1]["payload"])
        assert payload["client_capabilities"] == capabilities


def test_debug_set_is_processed_without_crash(client, app):
    """debug.set must be handled (not crash); server stays alive for the next message."""
    with client.websocket_connect("/ws") as ws:
        token = app.state.auth.mint_token(
            {"id": "u-001", "email": "admin@labmate.local", "role": "admin"}
        )
        ws.send_json({"type": "auth", "token": token})
        assert ws.receive_json()["type"] == "auth.ok"
        ev = ws.receive_json()
        while ev["type"] != "boot.ready":
            ev = ws.receive_json()

        # Create a session
        ws.send_json({"type": "session.new", "mode": "code"})
        created = ws.receive_json()
        while created["type"] != "session.updated":
            created = ws.receive_json()
        sid = created["session"]["id"]

        # Enable debug — server must NOT crash or disconnect
        ws.send_json({"type": "debug.set", "sessionId": sid, "enabled": True})

        # Prove server is still alive: open the session
        ws.send_json({"type": "session.open", "sessionId": sid})
        resp = ws.receive_json()
        assert resp["type"] == "session.updated"
        assert resp["session"]["id"] == sid


def test_relay_emits_reasoning_done_before_turn_done(client, app, redis):
    """_relay_task must synthesize reasoning.done from accumulated reasoning events."""
    import asyncio
    import json as _json

    token = app.state.auth.mint_token(
        {"id": "u-001", "email": "admin@labmate.local", "role": "admin"}
    )
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
        asyncio.get_event_loop().run_until_complete(
            redis.xadd(
                stream,
                {
                    "event": _json.dumps(
                        {
                            "type": "reasoning",
                            "task_id": task_id,
                            "seq": 1,
                            "node": "plan_node",
                            "text": "I think",
                        }
                    )
                },
            )
        )
        asyncio.get_event_loop().run_until_complete(
            redis.xadd(
                stream,
                {
                    "event": _json.dumps(
                        {
                            "type": "reasoning",
                            "task_id": task_id,
                            "seq": 2,
                            "node": "plan_node",
                            "text": " carefully",
                        }
                    )
                },
            )
        )
        asyncio.get_event_loop().run_until_complete(
            redis.xadd(
                stream,
                {
                    "event": _json.dumps(
                        {"type": "turn.done", "task_id": task_id, "seq": 3, "status": "complete"}
                    )
                },
            )
        )

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
