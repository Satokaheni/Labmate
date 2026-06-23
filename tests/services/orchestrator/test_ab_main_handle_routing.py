"""A/B routing — main._handle reads payload routing_mode and reports llm_calls.

Mirrors test_main_direct_answer.py's mock style. Asserts that _handle:
  - passes payload["routing_mode"] into orch.run_task(routing_mode=...)
  - defaults routing_mode to the env default when absent
  - includes an llm_calls count in the stored result
and that run_task seeds routing_mode into State. NEW file.
"""
from __future__ import annotations

import json

import pytest
from unittest.mock import AsyncMock

from services.orchestrator.main import OrchestratorProcess
from services.orchestrator import events as events_mod


def _make_storage():
    storage = AsyncMock()
    storage.workspaces = AsyncMock()
    storage.workspaces.record_session = AsyncMock()
    storage.workspaces.complete_session = AsyncMock()
    storage.workspaces.upsert_workspace = AsyncMock()
    return storage


@pytest.mark.asyncio
async def test_handle_passes_routing_mode_and_reports_llm_calls(monkeypatch):
    async def fake_emit(_type, **fields):
        pass

    monkeypatch.setattr(events_mod, "emit", fake_emit)

    proc = OrchestratorProcess()
    proc._redis = AsyncMock()

    orch = AsyncMock()
    orch.run_task.return_value = {
        "session_id": "s1", "goal_tree": {}, "direct_answer": True,
        "final_answer": "ok", "error": None,
    }
    orch.stream_final_answer = AsyncMock(return_value="x")

    storage = _make_storage()
    payload = json.dumps({
        "task_id": "ab-1", "task": "do x", "session_id": "s1", "routing_mode": "single",
    })
    await proc._handle("900-0", {"payload": payload}, orch, storage)

    # routing_mode forwarded to run_task.
    assert orch.run_task.await_args.kwargs.get("routing_mode") == "single"

    stored = json.loads(proc._redis.set.call_args[0][1])
    assert stored["ok"] is True
    assert "llm_calls" in stored
    assert isinstance(stored["llm_calls"], int)


@pytest.mark.asyncio
async def test_handle_defaults_routing_mode_when_absent(monkeypatch):
    async def fake_emit(_type, **fields):
        pass

    monkeypatch.setattr(events_mod, "emit", fake_emit)

    proc = OrchestratorProcess()
    proc._redis = AsyncMock()

    orch = AsyncMock()
    orch.run_task.return_value = {"session_id": "s2", "goal_tree": {}, "error": None}
    orch.stream_final_answer = AsyncMock(return_value="streamed")

    storage = _make_storage()
    payload = json.dumps({"task_id": "ab-2", "task": "do y", "session_id": "s2"})
    await proc._handle("901-0", {"payload": payload}, orch, storage)

    from services.orchestrator.main import ROUTING_MODE
    assert orch.run_task.await_args.kwargs.get("routing_mode") == ROUTING_MODE


@pytest.mark.asyncio
async def test_run_task_seeds_routing_mode_into_state(monkeypatch):
    """CodingOrchestrator.run_task seeds routing_mode into the initial State."""
    from services.orchestrator.coding_orchestrator import CodingOrchestrator

    orch = CodingOrchestrator.__new__(CodingOrchestrator)

    captured = {}

    class _Graph:
        async def ainvoke(self, initial, cfg):
            captured["initial"] = initial
            return initial

    orch.graph = _Graph()

    out = await orch.run_task("t", "s1", routing_mode="single")
    assert captured["initial"]["routing_mode"] == "single"
    assert out["routing_mode"] == "single"


@pytest.mark.asyncio
async def test_run_task_routing_mode_defaults(monkeypatch):
    from services.orchestrator.coding_orchestrator import CodingOrchestrator

    monkeypatch.delenv("ROUTING_MODE", raising=False)
    orch = CodingOrchestrator.__new__(CodingOrchestrator)

    class _Graph:
        async def ainvoke(self, initial, cfg):
            return initial

    orch.graph = _Graph()
    out = await orch.run_task("t", "s1")
    # Default flipped to "single" (multi remains via ROUTING_MODE=multi / payload override).
    assert out["routing_mode"] == "single"
