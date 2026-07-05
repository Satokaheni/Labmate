# tests/services/orchestrator/test_recovery_nudges.py
"""Integration tests for G3 recovery nudges wired into _run_react_loop:

  1. finish_reason=="length" (truncated output): a giant tool call truncated
     mid-arguments used to silently parse to args={} with no signal. Now
     detected, the truncated tool_calls are DROPPED (never dispatched), a
     bounded [RECOVERY] nudge is injected, and the loop continues.
  2. Malformed (invalid-JSON) tool-args streak: repeated invalid JSON used to
     silently default to args={}. Now, after LABMATE_MALFORMED_ARGS_LIMIT
     consecutive malformed parses, a [RECOVERY] nudge fires and the malformed
     call is NOT dispatched with {}.

Mirrors the harness in test_repeat_analysis_guard_loop.py / test_load_skill_
churn_bdd.py: a fake litellm-shaped model response sequence driven through
AsyncOrchestrator.react_execute (forced into the ReAct loop via SEQUENCING_MODE
="react"), with a FakeEmitter capturing emitted events for assertions.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.orchestrator import events
from services.orchestrator.coding_orchestrator import AsyncOrchestrator
from services.orchestrator.message_repair import validate_messages


def _response(
    tool_name: str | None,
    arguments,
    call_id: str = "call_1",
    *,
    finish_reason: str = "tool_calls",
    content: str | None = None,
):
    """A litellm-shaped response. `arguments` may be a dict (JSON-encoded) or a
    raw str (used as-is, so tests can inject deliberately-malformed/truncated
    JSON garbage)."""
    r = MagicMock()
    if tool_name is None:
        msg = MagicMock()
        msg.content = content or ""
        msg.tool_calls = None
        msg.reasoning_content = ""
        r.choices = [MagicMock(message=msg, finish_reason=finish_reason)]
        return r

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
    r.choices = [MagicMock(message=msg, finish_reason=finish_reason)]
    return r


class FakeEmitter:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def emit(self, type, **fields):
        self.events.append((type, fields))


def _build_orch():
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    orch.local_client = None

    class FakeContext:
        def get_manifest(self):
            return None

        def get_workspace_root(self):
            return None

    return orch, FakeContext()


async def _run_goal(monkeypatch, responses, goal="do something"):
    monkeypatch.setattr("services.orchestrator.coding_orchestrator.SEQUENCING_MODE", "react")
    orch, fake_ctx = _build_orch()
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
async def test_length_recovery_drops_truncated_call_and_nudges(monkeypatch):
    """finish_reason=='length' with a (garbage) tool_call: the truncated call is
    NOT dispatched, a recovery.truncated event fires, a [RECOVERY] nudge lands
    in messages, and the loop continues to a normal finish."""
    responses = [
        _response(
            "write_file",
            '{"path": "big.py", "content": "not really valid json truncated mid',
            call_id="c1",
            finish_reason="length",
        ),
        _response("finish", {"summary": "done"}, call_id="c2", finish_reason="tool_calls"),
    ]

    result, fake_emitter, mock_completion = await _run_goal(monkeypatch, responses)

    assert result["ok"] is True
    truncated_events = [e for e in fake_emitter.events if e[0] == "recovery.truncated"]
    assert len(truncated_events) == 1
    assert truncated_events[0][1].get("nudges") == 1

    # The truncated tool_call must never have been dispatched: no tool.start
    # event references it, and tools_used does not contain write_file.
    assert "write_file" not in result.get("tools_used", [])
    tool_starts = [e for e in fake_emitter.events if e[0] == "tool.start"]
    assert all(e[1].get("name") != "write_file" for e in tool_starts)

    # The nudge landed as a plain user message (no dangling tool_call).
    last_messages = (
        mock_completion.call_args_list[-1].kwargs.get("messages")
        or mock_completion.call_args_list[-1].args[0]
    )
    nudge_msgs = [
        m
        for m in last_messages
        if m.get("role") == "user" and "[RECOVERY]" in (m.get("content") or "")
    ]
    assert len(nudge_msgs) == 1
    assert "TRUNCATED" in nudge_msgs[0]["content"]

    # Message ordering stays valid. A "finish" tool_call legitimately never
    # gets a trailing tool-role result (the loop returns immediately on
    # finish, both before and after this change) — that is pre-existing
    # baseline behavior, not something these recovery branches introduce. So
    # assert no problems OTHER than that expected final dangling finish id.
    problems = [
        p
        for p in validate_messages(last_messages)
        if "dangling unanswered assistant tool_call id='c2'" not in p
    ]
    assert problems == []


@pytest.mark.asyncio
async def test_length_recovery_is_bounded(monkeypatch):
    """A model that returns finish_reason=='length' forever stops being nudged
    after LABMATE_MAX_LENGTH_NUDGES (default 2) and falls through to normal
    processing instead of nudging infinitely."""
    responses = [
        _response(
            "write_file",
            '{"path": "a.py", "content": "trunc',
            call_id="c1",
            finish_reason="length",
        ),
        _response(
            "write_file",
            '{"path": "a.py", "content": "trunc',
            call_id="c2",
            finish_reason="length",
        ),
        # Third "length" turn: MAX_LENGTH_NUDGES (2) already used, so this one
        # falls through to normal processing (not a 3rd nudge).
        _response("finish", {"summary": "done"}, call_id="c3", finish_reason="length"),
    ]

    result, fake_emitter, _mock_completion = await _run_goal(monkeypatch, responses)

    truncated_events = [e for e in fake_emitter.events if e[0] == "recovery.truncated"]
    assert len(truncated_events) == 2  # bounded at MAX_LENGTH_NUDGES, not 3
    assert result["ok"] is True  # 3rd turn fell through to normal 'finish' processing


@pytest.mark.asyncio
async def test_malformed_args_streak_nudges_and_skips_dispatch(monkeypatch):
    """Invalid-JSON tool args repeated LABMATE_MALFORMED_ARGS_LIMIT (default 2)
    times in a row: a recovery.malformed_args event fires, a [RECOVERY] nudge
    is injected, and the malformed call is never dispatched with args={}."""
    responses = [
        _response("run_bash", "{not valid json", call_id="c1", finish_reason="tool_calls"),
        _response("run_bash", "{also not valid", call_id="c2", finish_reason="tool_calls"),
        _response("finish", {"summary": "done"}, call_id="c3", finish_reason="tool_calls"),
    ]

    result, fake_emitter, mock_completion = await _run_goal(monkeypatch, responses)

    assert result["ok"] is True
    malformed_events = [e for e in fake_emitter.events if e[0] == "recovery.malformed_args"]
    assert len(malformed_events) == 1
    assert malformed_events[0][1].get("tool") == "run_bash"

    # The malformed call must not have been dispatched as a no-op {} run_bash:
    # no tool.done for run_bash should report a real dispatch result — instead
    # a synthetic error tool result was appended for its tool_call_id.
    last_messages = (
        mock_completion.call_args_list[-1].kwargs.get("messages")
        or mock_completion.call_args_list[-1].args[0]
    )
    tool_results = {
        m["tool_call_id"]: m["content"] for m in last_messages if m.get("role") == "tool"
    }
    assert "c2" in tool_results
    rejected = json.loads(tool_results["c2"])
    assert rejected.get("error") == "malformed_args"

    nudge_msgs = [
        m
        for m in last_messages
        if m.get("role") == "user" and "[RECOVERY]" in (m.get("content") or "")
    ]
    assert len(nudge_msgs) == 1
    assert "not valid JSON" in nudge_msgs[0]["content"]

    # Message ordering stays valid. As above, the final "finish" tool_call
    # (c3) legitimately never gets a trailing tool result — pre-existing
    # baseline behavior. Every OTHER declared tool_call id (notably c2, the
    # malformed one) must have been answered — no dangling ids beyond c3.
    problems = [
        p
        for p in validate_messages(last_messages)
        if "dangling unanswered assistant tool_call id='c3'" not in p
    ]
    assert problems == []


@pytest.mark.asyncio
async def test_no_false_trigger_on_normal_path(monkeypatch):
    """Regression guard: finish_reason=='stop'/'tool_calls' with valid JSON args
    never emits recovery events — both branches are no-ops on the normal path."""
    responses = [
        _response("run_bash", {"command": "echo hi"}, call_id="c1", finish_reason="tool_calls"),
        _response("finish", {"summary": "done"}, call_id="c2", finish_reason="stop"),
    ]

    result, fake_emitter, _mock_completion = await _run_goal(monkeypatch, responses)

    assert result["ok"] is True
    assert [e for e in fake_emitter.events if e[0] == "recovery.truncated"] == []
    assert [e for e in fake_emitter.events if e[0] == "recovery.malformed_args"] == []
