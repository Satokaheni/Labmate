# tests/services/orchestrator/test_find_and_fix_routing_bdd.py
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.edit_intent import requires_editing
from tests.conftest import run_async

pytestmark = [pytest.mark.bdd, pytest.mark.mocked]

scenarios("features/find_and_fix_routing.feature")


@pytest.fixture
def ctx():
    return {"enabled": True, "orch": None, "result": None,
            "skill_first_calls": 0, "loop_ran": False}


# ── Background / flag ──────────────────────────────────────────────────────
@given("the routing feature flag is on")
def _flag_on(ctx):
    ctx["enabled"] = True


@given("the routing feature flag is off")
def _flag_off(ctx):
    ctx["enabled"] = False


# ── Pure classifier scenarios ──────────────────────────────────────────────
@then(parsers.re(r'requires_editing for "(?P<goal>.*)" is (?P<expected>True|False)'))
def _requires_editing_is(ctx, goal, expected):
    # Regex parser extracts the goal and expected value directly.
    want = expected == "True"
    assert requires_editing(goal, enabled=ctx["enabled"]) is want, (goal, expected)


# ── Dispatcher wire-in scenarios ───────────────────────────────────────────
def _build_orch(ctx):
    from services.orchestrator.coding_orchestrator import AsyncOrchestrator
    router = MagicMock()
    router.runner = MagicMock()
    router.runner.reset_activations = MagicMock()
    router.run = AsyncMock(return_value={"ok": True, "result": "read-only review output"})
    orch = AsyncOrchestrator(skill_router=router, mcp=AsyncMock(), workspace="/tmp", max_steps=4)
    ctx["orch"] = orch
    return orch


@given("a skill_first orchestrator whose skill router would match a read-only review skill")
def _orch_readonly_match(ctx, monkeypatch):
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.SEQUENCING_MODE",
        "skill_first", raising=False,
    )
    _build_orch(ctx)


@given("a skill_first orchestrator whose skill router returns a successful read-only result")
def _orch_readonly_result(ctx, monkeypatch):
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.SEQUENCING_MODE",
        "skill_first", raising=False,
    )
    _build_orch(ctx)


@given("a fake model that reads, edits, and then finishes")
def _fake_model_edits(ctx):
    # The ReAct loop, if entered, gets a single 'finish' turn so it returns fast.
    finish_msg = MagicMock()
    finish_msg.content = None
    tc = MagicMock()
    tc.id = "c1"
    tc.function.name = "finish"
    tc.function.arguments = json.dumps({"summary": "read, edited, fixed"})
    finish_msg.tool_calls = [tc]
    ctx["_fake_resp"] = MagicMock(choices=[MagicMock(message=finish_msg)])


@when(parsers.parse('the goal "{goal}" is executed'))
def _execute(ctx, goal):
    orch = ctx["orch"]

    # Apply the flag-off case by env if the Background set it off.
    import os
    env_patch = {}
    if ctx["enabled"] is False:
        env_patch["ROUTE_EDIT_TO_REACT"] = "0"

    # Spy on the two paths.
    real_skill_first = orch._run_skill_first
    real_loop = orch._run_react_loop

    async def _spy_skill_first(g):
        ctx["skill_first_calls"] += 1
        return await real_skill_first(g)

    async def _spy_loop(g, n):
        ctx["loop_ran"] = True
        return await real_loop(g, n)

    resp = ctx.get("_fake_resp") or MagicMock(
        choices=[MagicMock(message=MagicMock(content="ok", tool_calls=None))]
    )

    async def _run():
        with patch.dict(os.environ, env_patch), \
             patch.object(orch, "_run_skill_first", side_effect=_spy_skill_first), \
             patch.object(orch, "_run_react_loop", side_effect=_spy_loop), \
             patch("services.orchestrator.coding_orchestrator.acompletion_with_failover",
                   new_callable=AsyncMock, return_value=resp):
            return await orch.react_execute(goal)

    ctx["result"] = run_async(_run())


@then("the single-skill fast-path was NOT taken")
def _fast_path_not_taken(ctx):
    assert ctx["skill_first_calls"] == 0


@then("the single-skill fast-path WAS taken")
def _fast_path_taken(ctx):
    assert ctx["skill_first_calls"] >= 1


@then("the multi-tool ReAct loop ran")
def _loop_ran(ctx):
    assert ctx["loop_ran"] is True


@then("react_execute returns ok True")
def _ok_true(ctx):
    assert ctx["result"]["ok"] is True


@then("the summary is the skill result")
def _summary_is_skill(ctx):
    assert "read-only review output" in ctx["result"]["summary"]
