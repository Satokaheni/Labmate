import pytest


async def _noop_boot(emit, checks, session_store=None):
    return None


def test_signal_registry_write_steer_stores_text():
    from services.orchestrator.inproc_bus import SignalRegistry

    sig = SignalRegistry()
    sig.write_steer("task-9", "switch to db.py")
    assert sig.read_and_clear_steer("task-9") == "switch to db.py"


class _FakeWS:
    """Minimal WebSocket double: scripts receive_json, records send_json."""

    def __init__(self, incoming):
        self._incoming = list(incoming)
        self.sent = []

    async def receive_json(self):
        if not self._incoming:
            from fastapi import WebSocketDisconnect

            raise WebSocketDisconnect()
        return self._incoming.pop(0)

    async def send_json(self, data):
        self.sent.append(data)


@pytest.mark.asyncio
async def test_ws_loop_steer_frame_writes_steer_signal(monkeypatch, runtime):
    from fastapi import WebSocketDisconnect

    import services.ws_gateway.server as server

    # Auth handshake frame, then a send (to set active_task_id), then a steer.
    incoming = [
        {"type": "auth", "token": "tok"},
        {"type": "send", "text": "do a thing", "sessionId": "s1"},
        {"type": "steer", "text": "actually, use db.py"},
    ]
    ws = _FakeWS(incoming)

    # Stub auth to accept, boot to no-op, and _handle_send to bind a known task_id.
    class _Auth:
        def verify_token(self, t):
            return {"sub": "u1", "email": "u@x", "role": "user"}

    monkeypatch.setattr(server, "run_boot_sequence", _noop_boot)

    async def _fake_handle_send(ws_, runtime_, msg_, **kw):
        import asyncio as _a

        async def _relay():
            return None

        return "task-steered", _a.create_task(_relay())

    monkeypatch.setattr(server, "_handle_send", _fake_handle_send)

    from services.ws_gateway.sessions import InMemorySessionStore

    try:
        await server._ws_loop(ws, _Auth(), runtime, {}, InMemorySessionStore())
    except WebSocketDisconnect:
        pass

    assert runtime.signals.read_and_clear_steer("task-steered") == "actually, use db.py"
