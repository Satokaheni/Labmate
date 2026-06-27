"""pytest-bdd step defs for replan_progress_guard.feature.

Binds Gherkin to the pure replan_should_stop guard and the wired replan loop.
Mocked only; no GPU, no services."""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.replan_guard import replan_should_stop, ReplanStop
from services.orchestrator.coding_orchestrator import AsyncOrchestrator
from tests.conftest import run_async

pytestmark = [pytest.mark.bdd, pytest.mark.mocked]

scenarios("features/replan_progress_guard.feature")


@pytest.fixture
def ctx():
    return {
        "history": [],
        "cap": 2,
        "decision": None,
        "run_calls": 0,
        "reset_calls": 0,
        "result": None,
    }


def _planner_msg(payload: dict):
    msg = MagicMock()
    msg.content = json.dumps(payload)
    msg.tool_calls = None
    msg.reasoning_content = ""
    return MagicMock(choices=[MagicMock(message=msg)])


# ── pure-guard scenarios ───────────────────────────────────────────────────
@given(parsers.parse('a replan history whose last step is "{step}"'))
def _hist_last(ctx, step):
    ctx["history"] = [{"step": step, "ok": True, "summary": "", "skills": []}]


@given(parsers.parse('a replan history that has used skill "{skill}" {n:d} times'))
def _hist_skill(ctx, skill, n):
    ctx["history"] = [
        {"step": f"use {skill} #{i}", "ok": True, "summary": "", "skills": [skill]}
        for i in range(n)
    ]


@given(parsers.parse("the skill repeat cap is {cap:d}"))
def _set_cap(ctx, cap):
    ctx["cap"] = cap


@when(parsers.parse('the planner proposes the next sub-goal "{nxt}"'))
def _propose(ctx, nxt):
    ctx["decision"] = replan_should_stop(nxt, ctx["history"], max_skill_repeats=ctx["cap"])


@then("the replan guard says stop")
def _says_stop(ctx):
    assert isinstance(ctx["decision"], ReplanStop)
    assert ctx["decision"].stop is True


@then("the replan guard says continue")
def _says_continue(ctx):
    assert ctx["decision"].stop is False


@then(parsers.parse('the replan stop reason is "{reason}"'))
def _stop_reason(ctx, reason):
    assert ctx["decision"].reason == reason


# ── wired-loop scenario ────────────────────────────────────────────────────
@given(parsers.parse('a replan orchestrator whose planner always asks to run "{skill}"'))
def _orch_repeat_planner(ctx, skill):
    runner = MagicMock()

    def _reset():
        ctx["reset_calls"] += 1

    runner.reset_activations = MagicMock(side_effect=_reset)
    runner.catalog_prompt = MagicMock(return_value="")
    skill_router = MagicMock()
    skill_router.runner = runner

    async def _run(subgoal):
        ctx["run_calls"] += 1
        return {"ok": True, "result": "located", "skill_name": skill}

    skill_router.run = AsyncMock(side_effect=_run)
    orch = AsyncOrchestrator(skill_router=skill_router, mcp=AsyncMock(), workspace="/tmp", max_steps=4)
    ctx["orch"] = orch
    ctx["skill"] = skill


@when(parsers.parse('the compound goal "{goal}" is executed in replan mode'))
def _exec_replan(ctx, goal, monkeypatch):
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.SEQUENCING_MODE", "replan", raising=False
    )
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.REPLAN_COMPOUND_GATE", False, raising=False
    )
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.REPLAN_MAX_SKILL_REPEATS", ctx["cap"], raising=False
    )
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.MAX_SEQ_STEPS", 6, raising=False
    )
    same = {"done": False, "next": f'Run {ctx["skill"]} on the module', "reason": ""}
    side = [_planner_msg(same) for _ in range(6)] + [_planner_msg({"summary": "done"})]

    async def _run():
        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock, side_effect=side,
        ):
            return await ctx["orch"].react_execute(goal)

    ctx["result"] = run_async(_run())


@then(parsers.parse("the matching skill is dispatched at most {n:d} times"))
def _dispatch_at_most(ctx, n):
    assert ctx["run_calls"] <= n


@then("the activation budget is reset at least once per executed sub-step")
def _reset_per_substep(ctx):
    # at least one reset per executed sub-step (run_calls), plus the per-goal reset.
    assert ctx["reset_calls"] >= ctx["run_calls"]
