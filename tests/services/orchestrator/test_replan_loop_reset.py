from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import services.orchestrator.coding_orchestrator as co
from services.orchestrator.coding_orchestrator import AsyncOrchestrator
from tests.conftest import run_async


def test_replan_max_skill_repeats_default_is_two():
    assert co.REPLAN_MAX_SKILL_REPEATS == 2


def _planner_msg(payload: dict):
    """A litellm-shaped response whose assistant content is JSON `payload`."""
    msg = MagicMock()
    msg.content = json.dumps(payload)
    msg.tool_calls = None
    msg.reasoning_content = ""
    return MagicMock(choices=[MagicMock(message=msg)])


def _make_orch_with_runner():
    runner = MagicMock()
    runner.reset_activations = MagicMock(return_value=None)
    runner.catalog_prompt = MagicMock(return_value="")
    skill_router = MagicMock()
    skill_router.runner = runner
    orch = AsyncOrchestrator(skill_router=skill_router, mcp=AsyncMock(), workspace="/tmp", max_steps=4)
    return orch, runner, skill_router


def test_replan_resets_activations_once_per_substep(monkeypatch):
    """Two real sub-steps -> reset_activations called for each sub-step (>=2),
    on top of the one per-goal reset in react_execute.
    """
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.SEQUENCING_MODE", "replan", raising=False
    )
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.REPLAN_COMPOUND_GATE", False, raising=False
    )
    orch, runner, skill_router = _make_orch_with_runner()

    # skill-first runs each sub-step and returns a distinct result.
    skill_router.run = AsyncMock(return_value={"ok": True, "result": "done", "skill_name": "test-gen"})

    # Planner: step1 -> "generate tests", step2 -> "fix the bug", then done.
    planner_payloads = [
        {"done": False, "next": "Generate and run unit tests", "reason": ""},
        {"done": False, "next": "Fix the off-by-one bug so tests pass", "reason": ""},
        {"done": True, "next": "", "reason": "complete"},
    ]
    # synth call returns plain content; reuse last planner shape via side_effect list.
    side = [_planner_msg(p) for p in planner_payloads] + [_planner_msg({"summary": "ok"})]

    async def _run():
        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock, side_effect=side,
        ):
            return await orch.react_execute("Generate tests AND fix the factorial bug")

    result = run_async(_run())
    assert isinstance(result, dict)
    # one per-goal reset (react_execute) + one per sub-step (2 sub-steps) = >= 3
    assert runner.reset_activations.call_count >= 3
