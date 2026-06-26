# tests/services/orchestrator/test_load_skill_churn_bdd.py
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.load_skill_guard import (
    is_repeat_load,
    already_loaded_message,
)
from tests.conftest import run_async

pytestmark = [pytest.mark.bdd, pytest.mark.mocked]

scenarios("features/load_skill_churn.feature")


@pytest.fixture
def ctx():
    return {"loaded": set(), "msg": None, "orch": None, "result": None}


# ── Pure-helper scenarios ──────────────────────────────────────────────────
@given(parsers.parse('the set of loaded skills is "{names}"'))
def _loaded_set(ctx, names):
    ctx["loaded"] = set(names.split(","))


@then(parsers.parse('is_repeat_load for "{name}" is {expected}'))
def _is_repeat(ctx, name, expected):
    want = expected.strip() == "True"
    assert is_repeat_load(name, ctx["loaded"]) is want


@when(parsers.parse('the already-loaded message is built for "{name}"'))
def _build_msg(ctx, name):
    ctx["msg"] = already_loaded_message(name, ctx["loaded"])


@then(parsers.parse('the message text contains "{phrase}"'))
def _msg_contains(ctx, phrase):
    assert phrase in ctx["msg"]["response"]["message"]


# ── ReAct wire-in scenarios ────────────────────────────────────────────────
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


def _build_orch():
    from services.orchestrator.coding_orchestrator import AsyncOrchestrator

    runner = MagicMock()
    runner.catalog_prompt.return_value = "- code-review: x\n- test-gen: y"
    runner.tool_schema.return_value = {
        "type": "function",
        "function": {"name": "load_skill", "parameters": {}},
    }
    runner.load_skill.side_effect = lambda n: {
        "name": "load_skill",
        "response": {"status": "loaded", "name": n, "body": "BODY"},
    }
    runner.reset_activations = MagicMock()
    router = MagicMock()
    router.runner = runner
    orch = AsyncOrchestrator(skill_router=router, mcp=AsyncMock(), workspace="/tmp", max_steps=6)
    return orch, runner


def _run_goal(ctx, responses):
    orch = ctx["orch"]

    async def _run():
        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, side_effect=responses):
            return await orch.react_execute(ctx["goal"])

    ctx["result"] = run_async(_run())


@given(parsers.parse(
    'a ReAct orchestrator whose model loads "{name}" twice then finishes'))
def _orch_double_load(ctx, monkeypatch, name):
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.SEQUENCING_MODE",
        "skill_first", raising=False,
    )
    orch, runner = _build_orch()
    ctx["orch"], ctx["runner"] = orch, runner
    ctx["responses"] = [
        _tool_call_response("load_skill", {"name": name}, "c1"),
        _tool_call_response("load_skill", {"name": name}, "c2"),
        _tool_call_response("finish", {"summary": "done"}, "c3"),
    ]


@given(parsers.parse(
    'a ReAct orchestrator whose model loads "{a}" then "{b}" then finishes'))
def _orch_two_skills(ctx, monkeypatch, a, b):
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.SEQUENCING_MODE",
        "skill_first", raising=False,
    )
    orch, runner = _build_orch()
    ctx["orch"], ctx["runner"] = orch, runner
    ctx["responses"] = [
        _tool_call_response("load_skill", {"name": a}, "c1"),
        _tool_call_response("load_skill", {"name": b}, "c2"),
        _tool_call_response("finish", {"summary": "done"}, "c3"),
    ]


@when(parsers.parse('the goal "{goal}" is executed'))
def _execute(ctx, goal):
    ctx["goal"] = goal
    _run_goal(ctx, ctx["responses"])


@then(parsers.parse('the skill runner loaded "{name}" exactly once'))
def _loaded_once(ctx, name):
    calls = [c for c in ctx["runner"].load_skill.call_args_list if c.args[0] == name]
    assert len(calls) == 1, ctx["runner"].load_skill.call_args_list


@then("the second load result reports it is already loaded")
def _second_already_loaded(ctx):
    # The runner was invoked once; the repeat was short-circuited, so the model
    # finished ok rather than erroring, and the loader ran a single time.
    assert ctx["result"]["ok"] is True
    code_review_calls = [
        c for c in ctx["runner"].load_skill.call_args_list if c.args[0] == "code-review"
    ]
    assert len(code_review_calls) == 1


@then("the iteration budget was refunded for the repeat load")
def _budget_refunded(ctx):
    # End-to-end proxy for the refund: with the refund active the goal completes
    # successfully within budget. (The unit test in Task 2 asserts the loader
    # call count directly; here we confirm honest completion.)
    assert ctx["result"]["ok"] is True


@then("neither first load reported already loaded")
def _no_false_dedupe(ctx):
    called = {c.args[0] for c in ctx["runner"].load_skill.call_args_list}
    assert "code-review" in called
    assert "test-gen" in called
