"""Step definitions for the wall-clock deadline + no-progress breaker feature.

Mirrors test_iteration_budget_bdd.py: patches litellm.acompletion with a
scripted side_effect list, plus injects a deterministic fake clock and sets the
two new env knobs (LABMATE_GOAL_DEADLINE_S, LABMATE_NOPROGRESS_LIMIT).
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.coding_orchestrator import AsyncOrchestrator
from tests.conftest import run_async

scenarios("features/wall_clock_idle_breaker.feature")


# ── helpers ────────────────────────────────────────────────────────────────

def _tool_call_msg(name: str, arguments: dict):
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
    return {
        "responses": [],
        "result": None,
        "mock": None,
        "advance": 0.0,
        "force_idle": False,
    }


# ── Background ───────────────────────────────────────────────────────────────

@given("an AsyncOrchestrator with no skill router and no mcp")
def _orch(ctx):
    # Fake monotonic clock: advances by ctx["advance"] seconds on each read.
    state = {"t": 0.0}

    def clock() -> float:
        v = state["t"]
        state["t"] += ctx["advance"]
        return v

    orch = AsyncOrchestrator(
        skill_router=None, mcp=None, workspace="/tmp", now=clock
    )
    mcp = AsyncMock()
    res = MagicMock()
    res.content = [MagicMock(text="output")]
    res.isError = False
    mcp.call_tool.return_value = res
    orch.mcp = mcp
    ctx["orch"] = orch


# ── Given steps ──────────────────────────────────────────────────────────────

@given(parsers.parse("the iteration budget cap is {cap:d}"))
def _set_cap(ctx, cap):
    ctx["orch"].max_steps = cap


@given(parsers.parse("the wall-clock deadline is {seconds:d} seconds"))
def _set_deadline(ctx, seconds, monkeypatch):
    monkeypatch.setenv("LABMATE_GOAL_DEADLINE_S", str(seconds))


@given(parsers.parse("the no-progress limit is {limit:d}"))
def _set_limit(ctx, limit, monkeypatch):
    monkeypatch.setenv("LABMATE_NOPROGRESS_LIMIT", str(limit))


@given(parsers.parse("the fake clock advances {seconds:d} seconds per turn"))
def _set_advance(ctx, seconds):
    ctx["advance"] = float(seconds)


@given(parsers.parse('the model calls run_bash with command "{command}" on turn {turn:d}'))
def _bash_on_turn(ctx, command, turn):
    _ensure_len(ctx, turn)
    ctx["responses"][turn - 1] = _tool_call_msg("run_bash", {"command": command})


@given(parsers.parse('the model calls finish with summary "{summary}" on turn {turn:d}'))
def _finish_on_turn(ctx, summary, turn):
    _ensure_len(ctx, turn)
    ctx["responses"][turn - 1] = _tool_call_msg("finish", {"summary": summary})


@given("the model returns an empty no-progress turn every turn")
def _always_idle(ctx):
    # A run_bash turn (keeps the loop alive) flagged as no-progress via the
    # _turn_made_progress seam below. Vary the command to avoid loop detection
    # (which would fire on repeated identical tool calls).
    ctx["force_idle"] = True
    commands = [f"noop{i}" for i in range(1, 21)]
    ctx["responses"] = [
        _tool_call_msg("run_bash", {"command": cmd}) for cmd in commands
    ]


def _ensure_len(ctx, turn):
    while len(ctx["responses"]) < turn:
        ctx["responses"].append(
            _tool_call_msg("run_bash", {"command": "echo filler"})
        )


# ── When step ────────────────────────────────────────────────────────────────

@when(parsers.parse('react_execute runs the goal "{goal}"'))
def _run(ctx, goal):
    patches = [
        patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock, side_effect=ctx["responses"],
        )
    ]
    if ctx["force_idle"]:
        patches.append(
            patch.object(AsyncOrchestrator, "_turn_made_progress", return_value=False)
        )

    with patches[0] as mock:
        if len(patches) > 1:
            with patches[1]:
                ctx["result"] = run_async(ctx["orch"].react_execute(goal))
        else:
            ctx["result"] = run_async(ctx["orch"].react_execute(goal))
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
    assert needle in ctx["result"]["summary"], (
        f"expected '{needle}' in summary, got: {ctx['result']['summary']}"
    )


@then(parsers.parse("the model was called exactly {n:d} times"))
def _called_n(ctx, n):
    assert ctx["mock"].await_count == n
