# tests/services/orchestrator/test_repeat_analysis_guard_loop.py
"""Integration test: repeat-analysis guard wired into _run_react_loop.

Mirrors the harness in test_load_skill_churn_bdd.py / test_coding_orchestrator.py's
run_tests pod-path test: a fake litellm-shaped model response sequence driven through
AsyncOrchestrator.react_execute, with skill_router.execute stubbed to record call
counts. Flag is default-OFF, so with ENABLE_REPEAT_ANALYSIS_GUARD unset the guard must
be a no-op (execute called for every call_skill_tool invocation, byte-identical to
pre-wiring behavior).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.orchestrator import events
from services.orchestrator.coding_orchestrator import AsyncOrchestrator


def _tool_call_response(tool_name: str, arguments: dict, call_id: str):
    """A litellm-shaped response that issues one tool call."""
    tc = MagicMock()
    tc.id = call_id
    tc.function = MagicMock()
    tc.function.name = tool_name
    tc.function.arguments = json.dumps(arguments)
    msg = MagicMock()
    msg.content = None
    msg.tool_calls = [tc]
    msg.reasoning_content = ""
    msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    return MagicMock(choices=[MagicMock(message=msg)])


class FakeEmitter:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def emit(self, type, **fields):
        self.events.append((type, fields))


def _build_orch_and_router():
    skill_router = MagicMock()
    skill_router.execute = AsyncMock(
        return_value={"ok": True, "result": {"findings": "looks fine"}}
    )
    orch = AsyncOrchestrator(skill_router=skill_router, mcp=None, workspace="/tmp")
    orch.local_client = None

    class FakeContext:
        def get_manifest(self):
            return None

        def get_workspace_root(self):
            return None

    return orch, skill_router, FakeContext()


CODE_REVIEW_ARGS = {"file": "src/app.py"}

RESPONSES = [
    _tool_call_response(
        "call_skill_tool",
        {"skill": "code-review", "tool": "review", "arguments": CODE_REVIEW_ARGS},
        "c1",
    ),
    _tool_call_response(
        "call_skill_tool",
        {"skill": "code-review", "tool": "review", "arguments": CODE_REVIEW_ARGS},
        "c2",
    ),
    _tool_call_response("finish", {"summary": "reviewed"}, "c3"),
]


async def _run_goal(monkeypatch, sequencing_mode="react"):
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.SEQUENCING_MODE", sequencing_mode
    )
    orch, skill_router, fake_ctx = _build_orch_and_router()
    monkeypatch.setattr("services.orchestrator.coding_orchestrator.client_context", fake_ctx)

    fake_emitter = FakeEmitter()
    with patch(
        "services.orchestrator.coding_orchestrator.acompletion_with_failover",
        new_callable=AsyncMock,
        side_effect=list(RESPONSES),
    ):
        token = events.current_emitter.set(fake_emitter)
        try:
            result = await orch.react_execute("review src/app.py twice")
        finally:
            events.current_emitter.reset(token)

    return result, skill_router, fake_emitter


@pytest.mark.asyncio
async def test_repeat_analysis_flag_off_executes_twice(monkeypatch):
    """Flag explicitly =0: guard is a no-op (the default is now ON, so this pins the off path)."""
    monkeypatch.setenv("ENABLE_REPEAT_ANALYSIS_GUARD", "0")

    result, skill_router, fake_emitter = await _run_goal(monkeypatch)

    assert skill_router.execute.call_count == 2, skill_router.execute.call_args_list
    deduped = [e for e in fake_emitter.events if e[0] == "analysis.deduped"]
    assert deduped == []
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_repeat_analysis_flag_on_dedupes_second_call(monkeypatch):
    """Flag ON: second identical code-review call is short-circuited with a steer."""
    monkeypatch.setenv("ENABLE_REPEAT_ANALYSIS_GUARD", "1")

    result, skill_router, fake_emitter = await _run_goal(monkeypatch)

    assert skill_router.execute.call_count == 1, skill_router.execute.call_args_list
    deduped = [e for e in fake_emitter.events if e[0] == "analysis.deduped"]
    assert len(deduped) == 1
    assert deduped[0][1].get("skill") == "code-review"
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_repeat_analysis_flag_on_second_result_has_steer_status(monkeypatch):
    """Flag ON: the second call_skill_tool tool-result content is the already_analyzed steer."""
    monkeypatch.setenv("ENABLE_REPEAT_ANALYSIS_GUARD", "1")
    monkeypatch.setattr("services.orchestrator.coding_orchestrator.SEQUENCING_MODE", "react")
    orch, skill_router, fake_ctx = _build_orch_and_router()
    monkeypatch.setattr("services.orchestrator.coding_orchestrator.client_context", fake_ctx)

    fake_emitter = FakeEmitter()
    with patch(
        "services.orchestrator.coding_orchestrator.acompletion_with_failover",
        new_callable=AsyncMock,
        side_effect=list(RESPONSES),
    ) as mock_completion:
        token = events.current_emitter.set(fake_emitter)
        try:
            await orch.react_execute("review src/app.py twice")
        finally:
            events.current_emitter.reset(token)

    # Inspect the messages passed into the final (finish) model call — it must
    # include the tool result for c2 with the already_analyzed steer status.
    last_call_messages = (
        mock_completion.call_args_list[-1].kwargs.get("messages")
        or mock_completion.call_args_list[-1].args[0]
    )
    tool_results = [m for m in last_call_messages if m.get("role") == "tool"]
    assert len(tool_results) == 2
    second_content = json.loads(tool_results[1]["content"])
    status = second_content.get("response", {}).get("status")
    assert status == "already_analyzed", second_content
