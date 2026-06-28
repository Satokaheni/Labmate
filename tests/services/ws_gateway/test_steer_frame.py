import pytest
import fakeredis.aioredis
from unittest.mock import AsyncMock

from services.ws_gateway.redis_bridge import write_steer, STEER_PREFIX


async def _noop_boot(emit, checks, session_store=None):
    return None


@pytest.mark.asyncio
async def test_write_steer_sets_key_with_text():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await write_steer(r, "task-9", "switch to db.py")
    assert await r.get(f"{STEER_PREFIX}task-9") == "switch to db.py"
    assert await r.ttl(f"{STEER_PREFIX}task-9") > 0


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
async def test_ws_loop_steer_frame_writes_steer_key(monkeypatch):
    import services.ws_gateway.server as server
    from services.ws_gateway.redis_bridge import STEER_PREFIX
    from fastapi import WebSocketDisconnect

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)

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
    monkeypatch.setattr(server, "run_boot_sequence",
                         AsyncMock() if False else _noop_boot)

    async def _fake_handle_send(ws_, redis_, msg_, **kw):
        import asyncio as _a
        done = _a.get_event_loop().create_future()
        done.set_result(None)
        async def _relay():
            return None
        return "task-steered", _a.create_task(_relay())

    monkeypatch.setattr(server, "_handle_send", _fake_handle_send)

    from services.ws_gateway.sessions import InMemorySessionStore
    try:
        await server._ws_loop(ws, _Auth(), r, {}, InMemorySessionStore())
    except WebSocketDisconnect:
        pass

    assert await r.get(f"{STEER_PREFIX}task-steered") == "actually, use db.py"
