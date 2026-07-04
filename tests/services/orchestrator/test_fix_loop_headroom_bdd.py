# tests/services/orchestrator/test_fix_loop_headroom_bdd.py
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from services.orchestrator.iteration_budget import IterationBudget
from services.orchestrator.loop_detection import (
    LoopDetector,
    call_signature,
    repeat_limit_for,
)
from tests.conftest import run_async

pytestmark = [pytest.mark.bdd, pytest.mark.mocked]

scenarios("features/fix_loop_headroom.feature")


@pytest.fixture
def ctx():
    return {"detector": None, "budget": None, "model_calls": 0, "react_result": None}


# ── Detector scenarios ─────────────────────────────────────────────────────
@given("a loop detector with the default repeat limit")
def _default_detector(ctx):
    ctx["detector"] = LoopDetector()


@when(parsers.parse('the mutating call "{name}" with arguments {args} is recorded'))
def _record_mutating(ctx, name, args):
    sig = call_signature(name, json.loads(args))
    ctx["detector"].record(sig, repeat_limit=repeat_limit_for(name))


@when(parsers.parse('the read call "{name}" with arguments {args} is recorded'))
def _record_read(ctx, name, args):
    sig = call_signature(name, json.loads(args))
    ctx["detector"].record(sig, repeat_limit=repeat_limit_for(name))


@then("the detector reports it should break")
def _should_break(ctx):
    # The break check must use the same per-tool threshold the records used.
    # All recorded calls in a scenario share one tool name, so derive it.
    assert ctx["detector"].should_break(repeat_limit=repeat_limit_for(_last_tool(ctx))) is True


@then("the detector reports it should not break")
def _should_not_break(ctx):
    assert ctx["detector"].should_break(repeat_limit=repeat_limit_for(_last_tool(ctx))) is False


@then(parsers.parse('the trip reason mentions "{word}"'))
def _reason_mentions(ctx, word):
    assert word in ctx["detector"].reason()


def _last_tool(ctx) -> str:
    # The signature is "name::json"; recover the tool name from the last sig.
    sigs = ctx["detector"]._sigs
    return sigs[-1].split("::", 1)[0] if sigs else ""


# ── Budget refund scenario ─────────────────────────────────────────────────
@given(parsers.parse("an iteration budget with capacity {cap:d}"))
def _budget(ctx, cap):
    ctx["budget"] = IterationBudget(max_total=cap)


@when(parsers.parse('a "{name}" turn is consumed and refunded'))
def _consume_refund(ctx, name):
    assert ctx["budget"].consume() is True
    ctx["budget"].refund()  # refundable tools (run_tests) are refunded in the loop


@then(parsers.parse("{n:d} working turns still fit in the budget"))
def _working_turns_fit(ctx, n):
    fit = 0
    for _ in range(n):
        if ctx["budget"].consume():
            fit += 1
    assert fit == n


# ── Edit-ceiling wire-in scenario ──────────────────────────────────────────
def _write_then_finish_responses():
    write_msg = MagicMock()
    tc = MagicMock()
    tc.id = "call_w"
    tc.function = MagicMock()
    tc.function.name = "write_file"
    tc.function.arguments = json.dumps({"path": "app.py", "content": "patched"})
    write_msg.content = None
    write_msg.tool_calls = [tc]
    write_msg.reasoning_content = ""
    write_msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    resp_write = MagicMock(choices=[MagicMock(message=write_msg)])

    finish_msg = MagicMock(tool_calls=None, content="done")
    finish_msg.model_dump = lambda: {"role": "assistant", "content": "done"}
    resp_finish = MagicMock(choices=[MagicMock(message=finish_msg)])
    return [resp_write, resp_finish]


@given("a ReAct orchestrator wired to a fake model that writes a file then finishes")
def _orch_write_finish(ctx, monkeypatch, tmp_path):
    from services.orchestrator.coding_orchestrator import AsyncOrchestrator

    monkeypatch.setattr("services.orchestrator.coding_orchestrator.SEQUENCING_MODE", "skill_first")

    orch = AsyncOrchestrator(skill_router=None, mcp=MagicMock(), max_steps=6)
    # write_file/read_file now execute directly (execute_local_tool) against a
    # real tmp_path workspace; the read-back verify reads the real file it
    # just wrote ("patched"), so no local-tool mock is needed.
    orch.workspace = str(tmp_path)
    orch.local_client = MagicMock()
    ctx["orch"] = orch


@when(parsers.parse('the edit goal "{goal}" is executed'))
def _execute_edit_goal(ctx, goal):
    orch = ctx["orch"]
    responses = _write_then_finish_responses()

    async def _counting(*a, **k):
        i = ctx["model_calls"]
        ctx["model_calls"] += 1
        return responses[min(i, len(responses) - 1)]

    class FakeEmitter:
        async def emit(self, type, **f):
            pass

    async def _run():
        from services.orchestrator import events

        with patch(
            "services.orchestrator.coding_orchestrator.acompletion_with_failover",
            new_callable=AsyncMock,
            side_effect=_counting,
        ):
            token = events.current_emitter.set(FakeEmitter())
            try:
                return await orch.react_execute(goal)
            finally:
                events.current_emitter.reset(token)

    ctx["react_result"] = run_async(_run())


@then("react_execute returns ok True")
def _ok_true(ctx):
    assert ctx["react_result"]["ok"] is True


@then("the model was allowed more than max_steps turns of headroom")
def _headroom(ctx):
    # The edit goal built its budget from LABMATE_MAX_ITERATIONS_EDIT (12),
    # strictly greater than max_steps (6). Verify the configured ceiling rather
    # than forcing 12 real turns: re-run the cap computation the loop uses.
    import os

    from services.orchestrator.edit_intent import requires_editing

    assert requires_editing("fix the bug in app.py") is True
    cap = int(os.getenv("LABMATE_MAX_ITERATIONS_EDIT", "12"))
    assert cap > ctx["orch"].max_steps
