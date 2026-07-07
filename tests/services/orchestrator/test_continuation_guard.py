# tests/services/orchestrator/test_continuation_guard.py
"""Integration tests for the G4 continuation guard wired into _run_react_loop.

Gap closed: a TEXT-ONLY turn (no tool_calls, no `finish` tool call) used to
return `ok=True` immediately at the top of the loop, BYPASSING the
verification-stop check that only runs in the `finish` branch. So a model that
edited files and then just answered in prose ("done, I fixed it") escaped
verification entirely — the exact fabrication verification_stop exists to kill.

G4 routes the text-only return through `needs_verification(...)`: if files were
edited without a passing run and nudge budget remains, a verify nudge
(source="text_only_return") is injected and the loop continues; otherwise the
text answer is returned normally (pure-answer goals and spent-budget stops are
unaffected).

Mirrors the harness in test_recovery_nudges.py: a fake litellm-shaped response
sequence driven through AsyncOrchestrator.react_execute (forced into the ReAct
loop via SEQUENCING_MODE="react"), with a FakeEmitter capturing events.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.orchestrator import events
from services.orchestrator.coding_orchestrator import AsyncOrchestrator


def _tool_response(tool_name, arguments, call_id="call_1", *, content=None):
    """A litellm-shaped response carrying one tool call."""
    tc = MagicMock()
    tc.id = call_id
    tc.function = MagicMock()
    tc.function.name = tool_name
    tc.function.arguments = arguments if isinstance(arguments, str) else json.dumps(arguments)
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = [tc]
    msg.reasoning_content = ""
    msg.model_dump = lambda: {"role": "assistant", "content": content or "", "tool_calls": []}
    r = MagicMock()
    r.choices = [MagicMock(message=msg, finish_reason="tool_calls")]
    return r


def _text_response(content):
    """A litellm-shaped TEXT-ONLY response (no tool calls)."""
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = None
    msg.reasoning_content = ""
    msg.model_dump = lambda: {"role": "assistant", "content": content or "", "tool_calls": []}
    r = MagicMock()
    r.choices = [MagicMock(message=msg, finish_reason="stop")]
    return r


class FakeEmitter:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def emit(self, type, **fields):
        self.events.append((type, fields))


def _build_orch(workspace):
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace=str(workspace))
    # Truthy sentinel: gates the local-tool branch on; the actual file I/O goes
    # through execute_local_tool(workspace=_tool_workspace()), not this object.
    orch.local_client = object()

    class FakeContext:
        def get_manifest(self):
            return None

        def get_workspace_root(self):
            return None

    return orch, FakeContext()


async def _run_goal(monkeypatch, responses, workspace, goal="review and fix the bug"):
    monkeypatch.setattr("services.orchestrator.coding_orchestrator.SEQUENCING_MODE", "react")
    orch, fake_ctx = _build_orch(workspace)
    monkeypatch.setattr("services.orchestrator.coding_orchestrator.client_context", fake_ctx)

    fake_emitter = FakeEmitter()
    with patch(
        "services.orchestrator.coding_orchestrator.acompletion_with_failover",
        new_callable=AsyncMock,
        side_effect=list(responses),
    ) as mock_completion:
        token = events.current_emitter.set(fake_emitter)
        try:
            result = await orch.react_execute(goal)
        finally:
            events.current_emitter.reset(token)

    return result, fake_emitter, mock_completion


@pytest.mark.asyncio
async def test_text_only_return_after_edit_nudges_then_gates_fabrication(monkeypatch, tmp_path):
    """Edited a file, then claimed success in prose WITHOUT verifying: the
    text-only return must (a) inject a verify nudge (source="text_only_return")
    and continue rather than accept ok=True, and (b) when the nudge budget is
    spent, gate the unverified "tests pass" CLAIM to ok=False with an honesty
    annotation — parity with the finish branch, closing the fabrication hole."""
    monkeypatch.setenv("MAX_VERIFY_NUDGES", "1")

    responses = [
        # 1) A real edit → edited_files={f.py}, tests_passed stays False.
        _tool_response("write_file", {"path": "f.py", "content": "x = 1\n"}, call_id="c1"),
        # 2) Text-only claim — unverified. G4: needs_verification True (budget 0<1)
        #    → verify nudge, continue (does NOT return here).
        _text_response("Done — I fixed the bug."),
        # 3) Text-only claim again — nudge budget spent (1==1) → returns via the
        #    parity path, which reconciles the unbacked success claim to ok=False.
        _text_response("All tests pass, I fixed it."),
    ]

    result, fake_emitter, _mock = await _run_goal(monkeypatch, responses, tmp_path)

    # Exactly one text-only-sourced verify nudge fired (the continuation guard).
    text_nudges = [
        e
        for e in fake_emitter.events
        if e[0] == "verify.nudge" and e[1].get("source") == "text_only_return"
    ]
    assert len(text_nudges) == 1
    assert text_nudges[0][1].get("files") == ["f.py"]

    # The edit really landed (real write-back), proving edited_files was populated.
    assert (tmp_path / "f.py").read_text() == "x = 1\n"

    # Spent-budget return: the unverified success claim is GATED (not a fake ok),
    # carries the verification-stop annotation, and reports tests_passed=False.
    assert result["ok"] is False
    assert result["tests_passed"] is False
    assert "verification-stop" in result["summary"]


@pytest.mark.asyncio
async def test_pure_answer_goal_returns_without_nudge(monkeypatch, tmp_path):
    """A goal that makes NO edits and answers in prose returns immediately with
    no verify nudge — the guard must be a no-op when nothing was edited."""
    responses = [_text_response("The answer is 42.")]

    result, fake_emitter, _mock = await _run_goal(
        monkeypatch, responses, tmp_path, goal="what is the answer?"
    )

    assert result["ok"] is True
    assert result["summary"] == "The answer is 42."
    assert [e for e in fake_emitter.events if e[0] == "verify.nudge"] == []
