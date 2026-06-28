# tests/services/orchestrator/test_ok_reconciliation_bdd.py
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.coding_orchestrator import AsyncOrchestrator
from tests.conftest import run_async

scenarios("features/ok_reconciliation.feature")


def _finish_msg(summary: str):
    tc = MagicMock()
    tc.id = "call-finish"
    tc.function = MagicMock()
    tc.function.name = "finish"
    tc.function.arguments = json.dumps({"summary": summary})
    msg = MagicMock()
    msg.tool_calls = [tc]
    msg.content = ""
    msg.reasoning_content = ""
    msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    return MagicMock(choices=[MagicMock(message=msg)])


@pytest.fixture
def ctx():
    return {"responses": [], "result": None}


@given("a reconciliation AsyncOrchestrator with no skill router and no mcp")
def _orch(ctx):
    ctx["orch"] = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")


@given(parsers.parse('the model calls finish with summary "{summary}" on turn {turn:d}'))
def _finish_on_turn(ctx, summary, turn):
    while len(ctx["responses"]) < turn:
        ctx["responses"].append(_finish_msg("filler"))
    ctx["responses"][turn - 1] = _finish_msg(summary)


@when(parsers.parse('the reconciliation loop runs the goal "{goal}"'))
def _run(ctx, goal):
    async def _go():
        with patch(
            "services.orchestrator.coding_orchestrator.acompletion_with_failover",
            new_callable=AsyncMock,
            side_effect=ctx["responses"],
        ):
            return await ctx["orch"]._run_react_loop(goal, 4)

    ctx["result"] = run_async(_go())


@then(parsers.parse("the reconciled ok is {value}"))
def _ok_is(ctx, value):
    assert ctx["result"]["ok"] is (value == "True")


@then(parsers.parse('the reconciled summary contains "{needle}"'))
def _summary_contains(ctx, needle):
    assert needle.lower() in ctx["result"]["summary"].lower()


@then(parsers.parse('the reconciled summary does not contain "{needle}"'))
def _summary_not_contains(ctx, needle):
    assert needle.lower() not in ctx["result"]["summary"].lower()
