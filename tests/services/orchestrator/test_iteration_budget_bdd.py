"""Step definitions for the BDD iteration budget feature.

Covers grace-turn semantics and cheap-call refunds in the ReAct loop.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.coding_orchestrator import AsyncOrchestrator
from tests.conftest import run_async

scenarios("features/iteration_budget.feature")


# ── helpers ────────────────────────────────────────────────────────────────

def _tool_call_msg(name: str, arguments: dict):
    """A litellm-style assistant message that calls a single tool."""
    tc = MagicMock()
    tc.id = f"call-{name}"
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = json.dumps(arguments)
    msg = MagicMock()
    msg.tool_calls = [tc]
    msg.content = ""
    msg.reasoning_content = ""
    msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    return MagicMock(choices=[MagicMock(message=msg)])


@pytest.fixture
def ctx():
    """Mutable scenario context: orchestrator, scripted responses, result."""
    return {"cap": 6, "responses": [], "result": None, "mock": None}


# ── Background ───────────────────────────────────────────────────────────────

@given("an AsyncOrchestrator with no skill router and no mcp")
def _orch(ctx):
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    # A stub MCP so run_bash returns output without a real bridge.
    mcp = AsyncMock()
    mcp_result = MagicMock()
    mcp_result.content = [MagicMock(text="output")]
    mcp_result.isError = False
    mcp.call_tool.return_value = mcp_result
    orch.mcp = mcp
    ctx["orch"] = orch


# ── Given steps ──────────────────────────────────────────────────────────────

@given(parsers.parse("the iteration budget cap is {cap:d}"))
def _set_cap(ctx, cap):
    ctx["cap"] = cap
    ctx["orch"].max_steps = cap


@given(parsers.parse('the model calls finish on its first turn with summary "{summary}"'))
def _finish_first(ctx, summary):
    ctx["responses"] = [_tool_call_msg("finish", {"summary": summary})]


@given(parsers.parse('every model turn calls run_bash with command "{command}"'))
def _always_bash(ctx, command):
    # Enough scripted responses to cover cap + grace without StopIteration.
    ctx["responses"] = [
        _tool_call_msg("run_bash", {"command": command}) for _ in range(20)
    ]


@given(parsers.parse('the model calls run_bash with command "{command}" on turn {turn:d}'))
def _bash_on_turn(ctx, command, turn):
    _ensure_len(ctx, turn)
    ctx["responses"][turn - 1] = _tool_call_msg("run_bash", {"command": command})


@given(parsers.parse('the model calls list_dir with path "{path}" on turn {turn:d}'))
def _list_dir_on_turn(ctx, path, turn):
    _ensure_len(ctx, turn)
    ctx["responses"][turn - 1] = _tool_call_msg("list_dir", {"path": path})


@given(parsers.parse('the model calls write_file with path "{path}" and content "{content}" on turn {turn:d}'))
def _write_file_on_turn(ctx, path, content, turn):
    _ensure_len(ctx, turn)
    ctx["responses"][turn - 1] = _tool_call_msg("write_file", {"path": path, "content": content})


@given(parsers.parse('the model calls finish with summary "{summary}" on turn {turn:d}'))
def _finish_on_turn(ctx, summary, turn):
    _ensure_len(ctx, turn)
    ctx["responses"][turn - 1] = _tool_call_msg("finish", {"summary": summary})


@given(parsers.parse('the LABMATE_MAX_ITERATIONS env var is set to "{value}"'))
def _set_env_var(ctx, value, monkeypatch):
    monkeypatch.setenv("LABMATE_MAX_ITERATIONS", value)


def _ensure_len(ctx, turn):
    while len(ctx["responses"]) < turn:
        # Filler that should never actually be consumed in a well-formed scenario.
        ctx["responses"].append(_tool_call_msg("run_bash", {"command": "echo filler"}))


# ── When step ────────────────────────────────────────────────────────────────

@when(parsers.parse('react_execute runs the goal "{goal}"'))
def _run(ctx, goal):
    with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
               new_callable=AsyncMock, side_effect=ctx["responses"]) as mock:
        ctx["result"] = run_async(
            ctx["orch"].react_execute(goal)
        )
        ctx["mock"] = mock


# ── Then steps ───────────────────────────────────────────────────────────────

@then("the result ok is True")
def _ok_true(ctx):
    assert ctx["result"]["ok"] is True


@then("the result ok is False")
def _ok_false(ctx):
    assert ctx["result"]["ok"] is False


@then(parsers.parse('the result summary contains "{needle}"'))
def _summary_contains(ctx, needle):
    assert needle in ctx["result"]["summary"]


@then(parsers.parse('the result summary contains either "{needle1}" or "{needle2}"'))
def _summary_contains_either(ctx, needle1, needle2):
    summary = ctx["result"]["summary"]
    assert needle1 in summary or needle2 in summary, \
        f"Expected summary to contain either '{needle1}' or '{needle2}', but got: {summary}"


@then(parsers.parse("the budget used count is {n:d}"))
def _used_is(ctx, n):
    # One model call per consumed unit (no refunds in the finish-first scenario).
    assert ctx["mock"].await_count == n


@then(parsers.parse("the model was called exactly {n:d} times"))
def _called_n(ctx, n):
    assert ctx["mock"].await_count == n
