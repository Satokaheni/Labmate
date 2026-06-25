# tests/services/orchestrator/test_tool_loop_detection_bdd.py
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.loop_detection import LoopDetector, call_signature

scenarios("features/tool_loop_detection.feature")


# ── Shared mutable context for the detector scenarios ──────────────────────
@pytest.fixture
def ctx():
    return {"detector": None, "last_break": False, "model_calls": 0, "react_result": None}


@given("a loop detector with the default repeat limit")
def _default_detector(ctx):
    ctx["detector"] = LoopDetector()


@given(parsers.parse("a loop detector with repeat limit {limit:d}"))
def _detector_with_limit(ctx, limit):
    ctx["detector"] = LoopDetector(repeat_limit=limit)


@when(parsers.parse('the call "{name}" with arguments {args} is recorded'))
def _record_call(ctx, name, args):
    sig = call_signature(name, json.loads(args))
    ctx["last_break"] = ctx["detector"].record(sig)


@when(parsers.parse('the call "{name}" with arguments {args} is recorded {count:d} times'))
def _record_call_n(ctx, name, args, count):
    sig = call_signature(name, json.loads(args))
    for _ in range(count):
        ctx["last_break"] = ctx["detector"].record(sig)


@then("the detector reports it should break")
def _should_break(ctx):
    assert ctx["detector"].should_break() is True


@then("the detector reports it should not break")
def _should_not_break(ctx):
    assert ctx["detector"].should_break() is False


@then(parsers.parse('the trip reason mentions "{word}"'))
def _reason_mentions(ctx, word):
    assert word in ctx["detector"].reason()


@then(parsers.parse("the detector should_break is {result}"))
def _should_break_is(ctx, result):
    expected = result.strip() == "True"
    assert ctx["detector"].should_break() is expected


# ── Wire-in scenario: a fake model that always repeats one tool call ───────
def _repeat_tool_response():
    """A litellm-shaped response whose message always calls run_bash {command: ls}."""
    tc = MagicMock()
    tc.id = "call_loop"
    tc.function = MagicMock()
    tc.function.name = "run_bash"
    tc.function.arguments = json.dumps({"command": "ls"})
    msg = MagicMock()
    msg.content = None
    msg.tool_calls = [tc]
    msg.reasoning_content = ""
    msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    return MagicMock(choices=[MagicMock(message=msg)])


@given("a ReAct orchestrator wired to a fake model that always calls run_bash with the same arguments")
def _orch_with_looping_model(ctx):
    from services.orchestrator.coding_orchestrator import AsyncOrchestrator

    orch = AsyncOrchestrator(skill_router=None, mcp=AsyncMock(), workspace="/tmp", max_steps=6)
    bash_result = MagicMock()
    bash_result.content = [MagicMock(text="files")]
    bash_result.isError = False
    orch.mcp.call_tool = AsyncMock(return_value=bash_result)
    ctx["orch"] = orch


@when(parsers.parse('the goal "{goal}" is executed'))
def _execute_goal(ctx, goal):
    orch = ctx["orch"]

    async def _counting(*a, **k):
        ctx["model_calls"] += 1
        return _repeat_tool_response()

    async def _run():
        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, side_effect=_counting):
            return await orch.react_execute(goal)

    import asyncio
    ctx["react_result"] = asyncio.run(_run())


@then("react_execute returns ok False")
def _react_ok_false(ctx):
    assert ctx["react_result"]["ok"] is False


@then("the summary mentions a loop")
def _summary_mentions_loop(ctx):
    assert "loop" in ctx["react_result"]["summary"].lower()


@then("the model was called fewer times than max_steps")
def _fewer_than_max_steps(ctx):
    assert ctx["model_calls"] < ctx["orch"].max_steps
