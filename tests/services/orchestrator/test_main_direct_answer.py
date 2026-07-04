"""FIX 10 (B5) — _handle's direct-answer fast-path branch.

When the plan node took the direct-answer fast-path (final_state["direct_answer"] is
True and NOT awaiting_clarification), _handle must NOT call stream_final_answer (the
plan already produced final_answer); it surfaces the existing final_answer via
answer.delta/answer.done. The result stays ok=True.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from services.orchestrator import events as events_mod
from services.orchestrator.main import OrchestratorProcess


def _make_storage():
    storage = AsyncMock()
    storage.workspaces = AsyncMock()
    storage.workspaces.record_session = AsyncMock()
    storage.workspaces.complete_session = AsyncMock()
    storage.workspaces.upsert_workspace = AsyncMock()
    return storage


@pytest.mark.asyncio
async def test_handle_direct_answer_skips_stream_and_emits_answer(monkeypatch):
    emitted: list[tuple[str, dict]] = []

    async def fake_emit(_type, **fields):
        emitted.append((_type, fields))

    monkeypatch.setattr(events_mod, "emit", fake_emit)

    proc = OrchestratorProcess()

    answer = "The answer is 4."
    orch = AsyncMock()
    orch.run_task.return_value = {
        "session_id": "s1",
        "goal_tree": {},
        "direct_answer": True,
        "final_answer": answer,
        "error": None,
    }
    # If this were (wrongly) called it would re-answer the already-answered task.
    orch.stream_final_answer = AsyncMock(return_value="RE-ANSWERED")

    storage = _make_storage()
    payload = {"task_id": "da-1", "task": "What is 2+2?", "session_id": "s1"}
    await proc._handle(payload, orch, storage)

    # The plan already produced the answer -> no re-streaming.
    orch.stream_final_answer.assert_not_called()

    # answer.delta and answer.done were emitted with the existing final_answer.
    delta = [f for t, f in emitted if t == "answer.delta"]
    done = [f for t, f in emitted if t == "answer.done"]
    assert delta and delta[0]["text"] == answer
    assert done and done[0]["text"] == answer

    # Result stays ok=True and surfaces the direct answer (not a re-answer).
    stored = await proc.results.wait_result("da-1", timeout=1.0)
    assert stored["ok"] is True
    assert stored["state"]["final_answer"] == answer
    assert stored["state"]["final_answer"] != "RE-ANSWERED"


@pytest.mark.asyncio
async def test_handle_normal_path_unaffected(monkeypatch):
    """Regression: a non-direct, non-clarification state still streams its answer."""

    async def fake_emit(_type, **fields):
        pass

    monkeypatch.setattr(events_mod, "emit", fake_emit)

    proc = OrchestratorProcess()

    orch = AsyncMock()
    orch.run_task.return_value = {
        "session_id": "s2",
        "goal_tree": {},
        "error": None,
    }
    orch.stream_final_answer = AsyncMock(return_value="streamed")

    storage = _make_storage()
    payload = {"task_id": "norm-1", "task": "do something", "session_id": "s2"}
    await proc._handle(payload, orch, storage)

    orch.stream_final_answer.assert_awaited_once()
    stored = await proc.results.wait_result("norm-1", timeout=1.0)
    assert stored["ok"] is True
    assert stored["state"]["final_answer"] == "streamed"
