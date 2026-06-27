"""Final-answer reconciliation in _handle.

A goal whose skill returned ok=True (final_state["error"] is None) but whose
RENDERED final answer (post stream_final_answer) is a punt must be stored with
ok=False. Genuine successes and verified fixes stay ok=True. This is the third
reconciliation seam; it does not touch the skill-first / ReAct seams.
"""
from __future__ import annotations

import json

import pytest
from unittest.mock import AsyncMock

from services.orchestrator.main import (
    OrchestratorProcess,
    GOALS_STREAM,
    GOALS_GROUP,
)
from services.orchestrator import events as events_mod


def _make_storage():
    storage = AsyncMock()
    storage.workspaces = AsyncMock()
    storage.workspaces.record_session = AsyncMock()
    storage.workspaces.complete_session = AsyncMock()
    storage.workspaces.upsert_workspace = AsyncMock()
    return storage


def _stored_payload(proc):
    set_args = proc._redis.set.call_args[0]
    return json.loads(set_args[1])


@pytest.mark.asyncio
async def test_rendered_punt_flips_ok_to_false(monkeypatch):
    async def fake_emit(_type, **fields):
        pass
    monkeypatch.setattr(events_mod, "emit", fake_emit)

    proc = OrchestratorProcess()
    proc._redis = AsyncMock()

    # Skill path produced a clean state (no error) — the false-ok the A/B saw.
    orch = AsyncMock()
    orch.run_task.return_value = {
        "session_id": "s1",
        "goal_tree": {},
        "final_answer": "internal assembled answer",
        "error": None,
    }
    # The summarizer renders a PUNT into final_answer.
    orch.stream_final_answer = AsyncMock(
        return_value="I couldn't analyze the file because it is too large. Please share a snippet."
    )

    storage = _make_storage()
    payload = json.dumps({
        "task_id": "fa-punt-1",
        "task": "Find the bug in big_module.py",
        "session_id": "s1",
        "user_id": "u1",
        "workspace_id": "w1",
    })
    await proc._handle("800-0", {"payload": payload}, orch, storage)

    stored = _stored_payload(proc)
    assert stored["ok"] is False
    # The rendered punt is what was stored as the final answer.
    assert "too large" in stored["state"]["final_answer"].lower()
    # complete_session was told it failed.
    storage.workspaces.complete_session.assert_awaited()
    assert storage.workspaces.complete_session.call_args.kwargs.get("ok") is False
    proc._redis.xack.assert_awaited_once_with(GOALS_STREAM, GOALS_GROUP, "800-0")


@pytest.mark.asyncio
async def test_rendered_genuine_success_stays_ok_true(monkeypatch):
    async def fake_emit(_type, **fields):
        pass
    monkeypatch.setattr(events_mod, "emit", fake_emit)

    proc = OrchestratorProcess()
    proc._redis = AsyncMock()

    orch = AsyncMock()
    orch.run_task.return_value = {
        "session_id": "s1",
        "goal_tree": {},
        "final_answer": "internal",
        "error": None,
    }
    orch.stream_final_answer = AsyncMock(
        return_value="Here is the square function you asked for: def sq(x): return x*x"
    )

    storage = _make_storage()
    payload = json.dumps({
        "task_id": "fa-ok-1",
        "task": "Write a square function",
        "session_id": "s1",
    })
    await proc._handle("800-1", {"payload": payload}, orch, storage)

    stored = _stored_payload(proc)
    assert stored["ok"] is True


@pytest.mark.asyncio
async def test_preexisting_error_preserved_when_already_failed(monkeypatch):
    async def fake_emit(_type, **fields):
        pass
    monkeypatch.setattr(events_mod, "emit", fake_emit)

    proc = OrchestratorProcess()
    proc._redis = AsyncMock()

    orch = AsyncMock()
    orch.run_task.return_value = {
        "session_id": "s1",
        "goal_tree": {},
        "final_answer": "internal",
        "error": "1 subtask(s) failed: parse (error: boom)",
    }
    # Even a punt render: ok was already False; the original error stays.
    orch.stream_final_answer = AsyncMock(
        return_value="I could not process the file, it is too large."
    )

    storage = _make_storage()
    payload = json.dumps({
        "task_id": "fa-err-1",
        "task": "Parse the file",
        "session_id": "s1",
    })
    await proc._handle("800-2", {"payload": payload}, orch, storage)

    stored = _stored_payload(proc)
    assert stored["ok"] is False
    assert stored["state"]["error"] == "1 subtask(s) failed: parse (error: boom)"


@pytest.mark.asyncio
async def test_clarification_path_not_reconciled(monkeypatch):
    """Regression: a clarification question is not a punt-bearing answer path;
    awaiting_clarification states skip stream_final_answer and must not be
    flipped to ok=False by the new seam."""
    async def fake_emit(_type, **fields):
        pass
    monkeypatch.setattr(events_mod, "emit", fake_emit)

    proc = OrchestratorProcess()
    proc._redis = AsyncMock()

    orch = AsyncMock()
    orch.run_task.return_value = {
        "session_id": "s1",
        "goal_tree": {},
        "awaiting_clarification": True,
        "clarification_question": "Which file did you mean?",
        "error": None,
    }
    orch.stream_final_answer = AsyncMock(return_value="UNUSED")

    storage = _make_storage()
    payload = json.dumps({
        "task_id": "fa-clar-1",
        "task": "fix it",
        "session_id": "s1",
    })
    await proc._handle("800-3", {"payload": payload}, orch, storage)

    stored = _stored_payload(proc)
    assert stored["ok"] is True
    orch.stream_final_answer.assert_not_called()
