import pytest
from fastapi.testclient import TestClient

from services.ws_gateway.config import Config
from services.ws_gateway.server import build_app
from services.ws_gateway.sessions import InMemorySessionStore


@pytest.fixture
def cfg():
    return Config(
        jwt_secret="test-secret",
        admin_email="admin@labmate.local",
        admin_password="correct-horse",
        jwt_expiry_seconds=3600,
        cors_origins=("http://localhost:5173",),
        mongo_url="mongodb://localhost:27017",
    )


@pytest.fixture
async def app(cfg, runtime, seeded_store):
    # Inject the stub runtime, all-ready boot checks, and an explicit in-memory
    # session store so that tests never depend on whether Mongo is running.
    async def ready(**_):
        return ("ready", "ok", "")

    checks = {k: ready for k in ("brain", "nervous_system", "hands", "memory", "workspace")}
    return build_app(
        cfg,
        runtime=runtime,
        boot_checks=checks,
        user_store=seeded_store,
        session_store=InMemorySessionStore(),
    )


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


def test_send_pushes_task_and_relays_events(client, app, runtime):
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

        # a task must have been submitted to the runtime
        assert len(runtime.submitted) == 1
        payload = runtime.submitted[0]
        task_id = payload["task_id"]

        # simulate the orchestrator publishing events for that task on the bus
        runtime.bus.publish(
            f"events:{task_id}",
            {"type": "answer.delta", "task_id": task_id, "seq": 1, "text": "ok"},
        )
        runtime.bus.publish(
            f"events:{task_id}",
            {"type": "turn.done", "task_id": task_id, "seq": 2, "status": "complete"},
        )

        delta = ws.receive_json()
        assert delta == {"type": "answer.delta", "turnId": assistant_turn_id, "text": "ok"}
        done = ws.receive_json()
        assert done == {"type": "turn.done", "turnId": assistant_turn_id, "status": "complete"}


def test_tool_result_message_writes_to_bus(client, app, runtime):
    """A tool.result message from the client publishes a frame on the tool-results topic."""
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

        task_id = runtime.submitted[0]["task_id"]

        # subscribe to the tool-results topic like the local-tool fulfiller would (7c)
        from services.orchestrator.local_tools import TOOL_RESULTS_TOPIC_PREFIX

        sub = runtime.bus.subscribe(f"{TOOL_RESULTS_TOPIC_PREFIX}{task_id}")

        # send a tool.result (simulating Electron completing a local tool)
        ws.send_json(
            {
                "type": "tool.result",
                "toolRequestId": "req-42",
                "result": {"content": "file contents"},
                "error": None,
            }
        )

        frame = asyncio.get_event_loop().run_until_complete(
            asyncio.wait_for(sub.__anext__(), timeout=2.0)
        )
        assert frame["tool_request_id"] == "req-42"
        assert frame["result"] == {"content": "file contents"}
        assert frame["error"] is None
        sub.close()


def test_cancel_signals_runtime_and_emits_turn_done_error(client, app, runtime):
    """cancel must call runtime.signals.request_cancel and emit turn.done:error."""
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

        task_id = runtime.submitted[-1]["task_id"]

        # Cancel the assistant turn
        ws.send_json({"type": "cancel", "sessionId": "s1", "turnId": assistant_turn_id})
        resp = ws.receive_json()
        assert resp["type"] == "turn.done"
        assert resp["status"] == "error"
        assert resp["turnId"] == assistant_turn_id

    assert runtime.signals.is_cancelled(task_id) is True


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


def test_session_delete_emits_session_deleted(client, app):
    with client.websocket_connect("/ws") as ws:
        _boot_to_ready(ws, app)
        ws.send_json({"type": "session.new", "mode": "chat"})
        created = ws.receive_json()
        sid = created["session"]["id"]

        ws.send_json({"type": "session.delete", "sessionId": sid})
        deleted = ws.receive_json()
        assert deleted["type"] == "session.deleted"
        assert deleted["sessionId"] == sid


def test_session_delete_clears_active_session_when_deleted(client, app):
    with client.websocket_connect("/ws") as ws:
        _boot_to_ready(ws, app)
        ws.send_json({"type": "session.new", "mode": "chat"})
        created = ws.receive_json()
        sid = created["session"]["id"]

        # Open the session to make it active
        ws.send_json({"type": "session.open", "sessionId": sid})
        ws.receive_json()  # session.updated
        ws.receive_json()  # session.history

        # Delete the active session
        ws.send_json({"type": "session.delete", "sessionId": sid})
        deleted = ws.receive_json()
        assert deleted["type"] == "session.deleted"
        assert deleted["sessionId"] == sid


def test_session_delete_missing_sid_no_crash(client, app):
    with client.websocket_connect("/ws") as ws:
        _boot_to_ready(ws, app)
        # Deleting a nonexistent session should not crash the server
        ws.send_json({"type": "session.delete", "sessionId": "nonexistent"})
        # Server should remain alive and we should not receive a delete frame
        # (timeout would occur, so we don't assert on receive)


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


def test_session_open_replays_stored_turns(client, app):
    """session.open should replay a session's stored turns in a session.history frame."""
    import asyncio

    with client.websocket_connect("/ws") as ws:
        _boot_to_ready(ws, app)
        ws.send_json({"type": "session.new", "mode": "chat"})
        created = ws.receive_json()
        sid = created["session"]["id"]

        # Simulate adding turns to the store (inject directly for testing)
        # Use asyncio.run to execute async methods in this sync test context
        store = app.state.store
        asyncio.run(
            store.add_turn(
                sid,
                {
                    "id": "t-1",
                    "role": "user",
                    "text": "Hello",
                    "sessionId": sid,
                    "createdAt": "2026-01-01T00:00:00Z",
                    "status": "complete",
                },
            )
        )
        asyncio.run(
            store.add_turn(
                sid,
                {
                    "id": "t-2",
                    "role": "assistant",
                    "text": "Hi there",
                    "sessionId": sid,
                    "createdAt": "2026-01-01T00:00:01Z",
                    "status": "complete",
                },
            )
        )

        ws.send_json({"type": "session.open", "sessionId": sid})
        opened = ws.receive_json()
        assert opened["type"] == "session.updated"

        # Next frame should be session.history with the turns
        history = ws.receive_json()
        assert history["type"] == "session.history"
        assert history["sessionId"] == sid
        assert len(history["turns"]) == 2
        assert history["turns"][0]["id"] == "t-1"
        assert history["turns"][0]["text"] == "Hello"
        assert history["turns"][1]["id"] == "t-2"
        assert history["turns"][1]["text"] == "Hi there"


def test_title_from_message():
    from services.ws_gateway.server import _title_from_message

    assert _title_from_message("hello world") == "hello world"
    assert _title_from_message("") == "New session"
    assert _title_from_message("\n\n  first line  \nsecond") == "first line"
    out = _title_from_message("x" * 60)
    assert out.endswith("…") and len(out) <= 49


def test_send_auto_creates_and_titles_session(client, app, runtime):
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


def test_client_capabilities_frame_is_threaded_into_task_payload(client, app, runtime):
    """A client.capabilities frame followed by send results in submit_goal receiving the manifest."""
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

        # Verify submit_goal payload includes client_capabilities
        assert len(runtime.submitted) >= 1
        payload = runtime.submitted[-1]
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


def test_relay_emits_reasoning_done_before_turn_done(client, app, runtime):
    """_relay_task must synthesize reasoning.done from accumulated reasoning events."""
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

        task_id = runtime.submitted[-1]["task_id"]

        # Publish reasoning events + turn.done onto the events bus topic
        runtime.bus.publish(
            f"events:{task_id}",
            {
                "type": "reasoning",
                "task_id": task_id,
                "seq": 1,
                "node": "plan_node",
                "text": "I think",
            },
        )
        runtime.bus.publish(
            f"events:{task_id}",
            {
                "type": "reasoning",
                "task_id": task_id,
                "seq": 2,
                "node": "plan_node",
                "text": " carefully",
            },
        )
        runtime.bus.publish(
            f"events:{task_id}",
            {"type": "turn.done", "task_id": task_id, "seq": 3, "status": "complete"},
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
