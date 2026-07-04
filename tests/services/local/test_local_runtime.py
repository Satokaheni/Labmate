"""Offline integration test for the Piece 4 T7c co-located local harness.

Proves the single-process gateway+orchestrator loop end-to-end with NO GPU
and NO Redis: a FakeRuntime wires the REAL in-process bus/signal/result
primitives (services.orchestrator.inproc_bus) but replaces goal *processing*
with a scriptable `submit_goal`, driven through a real fastapi TestClient
websocket against the real gateway app (services.ws_gateway.server.build_app).

Cases:
  1. Relay/translate happy path (turn.start -> reasoning -> answer.delta -> turn.done)
  2. steer / cancel signal round-trip

(The former case 3 — tool.request local fulfillment via request_local_tool — was
removed in Piece 5 5d: local tools now execute directly via execute_local_tool,
so the tool.request bus round-trip no longer exists.)
"""

from __future__ import annotations

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from services.orchestrator.inproc_bus import EventBus, ResultRegistry, SignalRegistry
from services.ws_gateway.config import Config
from services.ws_gateway.server import build_app
from services.ws_gateway.user_store import InMemoryUserStore


class FakeRuntime:
    """Fake `runtime` for the gateway: real bus/signals/results, scripted goals.

    `submit_goal` replays a canned event script onto the bus (cases 1/2).
    """

    def __init__(self) -> None:
        self.bus = EventBus()
        self.signals = SignalRegistry()
        self.results = ResultRegistry()
        self.submitted: list[dict] = []
        self._script = None  # callable(task_id) -> list[event dicts]

    async def submit_goal(self, payload: dict) -> str:
        task_id = payload.get("task_id") or "t-test"
        self.submitted.append(payload)
        if self._script:
            for ev in self._script(task_id):
                self.bus.publish(f"events:{task_id}", ev)
        return task_id


def _cfg() -> Config:
    return Config(
        jwt_secret="s",
        admin_email="admin@labmate.local",
        admin_password="pw",
        jwt_expiry_seconds=3600,
        cors_origins=(),
        mongo_url="mongodb://localhost:27017",
    )


@pytest.fixture
async def seeded_store():
    store = InMemoryUserStore()
    ph = PasswordHasher()
    await store.create(
        email="admin@labmate.local",
        display_name="Admin",
        password_hash=ph.hash("correct-horse"),
        role="admin",
    )
    return store


def _ready_checks() -> dict:
    async def ready(**_):
        return ("ready", "ok", "")

    return {k: ready for k in ("brain", "nervous_system", "hands", "memory", "workspace")}


def _make_client(runtime, seeded_store) -> tuple[TestClient, object]:
    app = build_app(_cfg(), runtime=runtime, boot_checks=_ready_checks(), user_store=seeded_store)
    return TestClient(app), app


def _login_and_connect(c: TestClient):
    r = c.post("/auth/login", json={"email": "admin@labmate.local", "password": "correct-horse"})
    assert r.status_code == 200
    token = r.json()["token"]
    ws = c.websocket_connect("/ws")
    ws_ctx = ws.__enter__()
    ws_ctx.send_json({"type": "auth", "token": token})
    assert ws_ctx.receive_json()["type"] == "auth.ok"
    assert ws_ctx.receive_json()["type"] == "boot.plan"
    ev = ws_ctx.receive_json()
    while ev["type"] == "boot.update":
        ev = ws_ctx.receive_json()
    assert ev["type"] == "boot.ready"
    return ws, ws_ctx


def test_relay_translate_happy_path(seeded_store):
    runtime = FakeRuntime()

    def script(task_id: str) -> list[dict]:
        return [
            {"type": "turn.start", "task": "hi"},
            {"type": "reasoning", "text": "thinking...", "node": "chat_node"},
            {"type": "answer.delta", "text": "hello"},
            {"type": "turn.done", "status": "complete", "final_answer": "hello"},
        ]

    runtime._script = script
    c, _app = _make_client(runtime, seeded_store)
    ws, ws_ctx = _login_and_connect(c)
    try:
        ws_ctx.send_json({"type": "send", "text": "hi", "sessionId": ""})

        # turn.created (user), turn.created (assistant)
        ev = ws_ctx.receive_json()
        assert ev["type"] == "turn.created"
        ev = ws_ctx.receive_json()
        assert ev["type"] == "turn.created"

        seen_types = []
        ev = ws_ctx.receive_json()
        while ev["type"] != "turn.done":
            seen_types.append(ev["type"])
            ev = ws_ctx.receive_json()

        assert ev["type"] == "turn.done"
        assert "reasoning.done" in seen_types or "reasoning" in seen_types
        assert any(t in ("answer.delta", "answer") for t in seen_types)
        assert runtime.submitted, "submit_goal should have been called"
    finally:
        ws.__exit__(None, None, None)


def test_steer_then_cancel(seeded_store):
    runtime = FakeRuntime()
    # No script: the relay just waits: cancel() is sent explicitly by the test,
    # which cancels the relay task client-side without requiring turn.done.
    c, _app = _make_client(runtime, seeded_store)
    ws, ws_ctx = _login_and_connect(c)
    try:
        ws_ctx.send_json({"type": "send", "text": "do a thing", "sessionId": ""})
        ev = ws_ctx.receive_json()
        assert ev["type"] == "turn.created"
        ev = ws_ctx.receive_json()
        assert ev["type"] == "turn.created"

        task_id = runtime.submitted[-1]["task_id"]

        ws_ctx.send_json({"type": "steer", "text": "focus on X"})
        ack = ws_ctx.receive_json()
        assert ack["type"] == "steer.ack"
        assert ack["taskId"] == task_id
        assert runtime.signals.read_and_clear_steer(task_id) == "focus on X"

        ws_ctx.send_json({"type": "cancel", "turnId": "whatever"})
        done = ws_ctx.receive_json()
        assert done["type"] == "turn.done"
        assert done["status"] == "error"
        assert runtime.signals.is_cancelled(task_id)
    finally:
        ws.__exit__(None, None, None)
