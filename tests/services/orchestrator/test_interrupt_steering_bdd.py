from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from services.orchestrator import events
from services.orchestrator.coding_orchestrator import AsyncOrchestrator
from services.orchestrator.inproc_bus import SignalRegistry
from services.orchestrator.steer_inject import OOB_OPEN
from tests.conftest import run_async

pytestmark = [pytest.mark.bdd, pytest.mark.mocked]

scenarios("features/interrupt_steering.feature")


def _tool_resp(name, args, call_id="c"):
    tc = MagicMock()
    tc.id = call_id
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = json.dumps(args)
    msg = MagicMock()
    msg.content = None
    msg.tool_calls = [tc]
    msg.reasoning_content = ""
    msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    return MagicMock(choices=[MagicMock(message=msg)])


@pytest.fixture
def ctx():
    return {
        "signals": SignalRegistry(),
        "task_id": None,
        "responses": [],
        "steer_before_turn": None,
        "steer_text": None,
        "cancel_before_turn": None,
        "captured": [],
        "result": None,
    }


@given("a ReAct orchestrator wired to a fakeredis steer/cancel channel")
def _orch(ctx):
    orch = AsyncOrchestrator(skill_router=None, mcp=AsyncMock(), workspace="/tmp", max_steps=6)
    bash_result = MagicMock()
    bash_result.content = [MagicMock(text="files")]
    bash_result.isError = False
    orch.mcp.call_tool = AsyncMock(return_value=bash_result)
    orch.signals = ctx["signals"]
    ctx["orch"] = orch


@given(parsers.parse('the active task id is "{task_id}"'))
def _task_id(ctx, task_id):
    ctx["task_id"] = task_id


@given("the model will call run_bash then finish over two turns")
def _bash_then_finish(ctx):
    ctx["responses"] = [
        _tool_resp("run_bash", {"command": "ls"}, "c1"),
        _tool_resp("finish", {"summary": "done"}, "c2"),
    ]


@given("the model will call run_bash on every turn")
def _always_bash(ctx):
    ctx["responses"] = "ALWAYS_BASH"


@given("the model will call run_bash on three turns then finish")
def _three_bash_then_finish(ctx):
    ctx["responses"] = [
        _tool_resp("run_bash", {"command": "ls"}, "c1"),
        _tool_resp("run_bash", {"command": "pwd"}, "c2"),
        _tool_resp("run_bash", {"command": "whoami"}, "c3"),
        _tool_resp("finish", {"summary": "done"}, "c4"),
    ]


@given(parsers.parse('the model will call finish on the first turn with summary "{summary}"'))
def _finish_first(ctx, summary):
    ctx["responses"] = [_tool_resp("finish", {"summary": summary}, "c1")]


@given(parsers.parse('the user writes the steer "{text}" before the second turn'))
def _steer_before_second(ctx, text):
    ctx["steer_before_turn"] = 2
    ctx["steer_text"] = text


@given(parsers.parse('the user cancels task "{task_id}" before the second turn'))
def _cancel_before_second(ctx, task_id):
    ctx["cancel_before_turn"] = 2


@when(parsers.parse('react_execute runs the goal "{goal}"'))
def _run_goal(ctx, goal):
    orch = ctx["orch"]
    turn = {"n": 0}

    async def _model(*a, **k):
        turn["n"] += 1
        # Capture the messages this call saw (deep copy of the relevant shape).
        ctx["captured"].append(json.dumps(k["messages"], default=str, ensure_ascii=False))
        # Fire the scheduled steer/cancel BEFORE producing this turn's response,
        # so it is visible at the TOP of the NEXT turn.
        if ctx["steer_before_turn"] == turn["n"] + 1 and ctx["steer_text"]:
            await events.write_steer(ctx["signals"], ctx["task_id"], ctx["steer_text"])
        if ctx["cancel_before_turn"] == turn["n"] + 1:
            ctx["signals"].request_cancel(ctx["task_id"])
        if ctx["responses"] == "ALWAYS_BASH":
            return _tool_resp("run_bash", {"command": "ls"})
        return ctx["responses"].pop(0)

    em = events.EventEmitter(MagicMock(), ctx["task_id"])
    em.emit = AsyncMock()
    token = events.current_emitter.set(em)

    async def _go():
        with patch(
            "services.orchestrator.coding_orchestrator.acompletion_with_failover",
            new=AsyncMock(side_effect=_model),
        ):
            return await orch.react_execute(goal)

    try:
        ctx["result"] = run_async(_go())
    finally:
        events.current_emitter.reset(token)


@then("the messages sent on the second model call contain an out-of-band user message")
def _second_has_oob(ctx):
    assert len(ctx["captured"]) >= 2
    assert OOB_OPEN in ctx["captured"][1]


@then("that message wraps the steer text in the out-of-band marker")
def _wraps_steer(ctx):
    assert ctx["steer_text"] in ctx["captured"][1]


@then(parsers.parse('the steer key "{key}" is empty afterward'))
def _steer_key_empty(ctx, key):
    # Consume-once semantics on the SignalRegistry: a second read returns None.
    assert run_async(events.read_and_clear_steer(ctx["signals"], ctx["task_id"])) is None


@then("react_execute returns ok False")
def _ok_false(ctx):
    assert ctx["result"]["ok"] is False


@then("the summary mentions it was cancelled")
def _summary_cancel(ctx):
    assert "cancel" in ctx["result"]["summary"].lower()


@then("the model was called fewer times than max_steps")
def _fewer_calls(ctx):
    assert len(ctx["captured"]) < ctx["orch"].max_steps


@then("react_execute returns ok True")
def _ok_true(ctx):
    assert ctx["result"]["ok"] is True


@then(parsers.parse('the summary is "{summary}"'))
def _summary_is(ctx, summary):
    assert ctx["result"]["summary"] == summary


@then("no out-of-band user message was injected")
def _no_oob(ctx):
    assert all(OOB_OPEN not in blob for blob in ctx["captured"])


@then("exactly one model call carried an out-of-band user message")
def _exactly_one_oob(ctx):
    n = sum(1 for blob in ctx["captured"] if OOB_OPEN in blob)
    assert n == 1
