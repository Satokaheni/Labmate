from __future__ import annotations

import json

import pytest
from unittest.mock import AsyncMock

from services.orchestrator.main import (
    OrchestratorProcess,
    GOALS_STREAM,
    GOALS_GROUP,
)


def _make_storage():
    storage = AsyncMock()
    storage.workspaces = AsyncMock()
    storage.workspaces.record_session = AsyncMock()
    storage.workspaces.complete_session = AsyncMock()
    storage.workspaces.upsert_workspace = AsyncMock()
    return storage


@pytest.mark.asyncio
async def test_handle_clarification_does_not_call_stream_final_answer():
    """When the graph halts for clarification, _handle must NOT guess an answer:
    stream_final_answer is never called, and final_answer is set to the
    clarification_question. The result is still ok=True (clarification != error)."""
    proc = OrchestratorProcess()
    proc._redis = AsyncMock()

    question = "Do you want the function and test in one file or two?"
    orch = AsyncMock()
    orch.run_task.return_value = {
        "session_id": "s1",
        "goal_tree": {},
        "awaiting_clarification": True,
        "clarification_question": question,
        "error": None,
    }
    # If this were (wrongly) called it would overwrite final_answer with a guess.
    orch.stream_final_answer = AsyncMock(return_value="GUESSED ANSWER")

    storage = _make_storage()

    payload = json.dumps({"task_id": "clar-1", "task": "ambiguous task", "session_id": "s1"})
    await proc._handle("500-0", {"payload": payload}, orch, storage)

    # The guessing path must NOT run.
    orch.stream_final_answer.assert_not_called()

    # The written result surfaces the question, not a guess, and stays ok=True.
    set_args = proc._redis.set.call_args[0]
    stored = json.loads(set_args[1])
    assert stored["ok"] is True
    assert stored["state"]["final_answer"] == question
    assert stored["state"]["final_answer"] != "GUESSED ANSWER"

    proc._redis.xack.assert_awaited_once_with(GOALS_STREAM, GOALS_GROUP, "500-0")


@pytest.mark.asyncio
async def test_handle_normal_path_still_streams_final_answer():
    """Regression: when not awaiting clarification, stream_final_answer IS called
    and its returned text becomes final_answer (existing behavior preserved)."""
    proc = OrchestratorProcess()
    proc._redis = AsyncMock()

    orch = AsyncMock()
    orch.run_task.return_value = {
        "session_id": "s2",
        "goal_tree": {},
        "error": None,
    }
    orch.stream_final_answer = AsyncMock(return_value="the streamed answer")

    storage = _make_storage()

    payload = json.dumps({"task_id": "norm-1", "task": "do something", "session_id": "s2"})
    await proc._handle("600-0", {"payload": payload}, orch, storage)

    orch.stream_final_answer.assert_awaited_once()

    set_args = proc._redis.set.call_args[0]
    stored = json.loads(set_args[1])
    assert stored["ok"] is True
    assert stored["state"]["final_answer"] == "the streamed answer"


@pytest.mark.asyncio
async def test_stream_yields_clarification_question_when_awaiting():
    """coding_orchestrator.stream() must yield the clarification_question (not a
    guessed/empty final answer) when run_task halts for clarification."""
    from services.orchestrator.coding_orchestrator import CodingOrchestrator

    orch = CodingOrchestrator.__new__(CodingOrchestrator)
    orch.graph = object()  # non-None so the guard passes

    question = "Which language should the implementation use?"

    async def fake_run_task(prompt, session_id, user_id="", workspace_id=""):
        return {
            "awaiting_clarification": True,
            "clarification_question": question,
            "goal_tree": {},
            "final_answer": "",
        }

    orch.run_task = fake_run_task

    chunks = [c async for c in orch.stream("ambiguous prompt")]
    assert chunks == [question]
