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


def test_replan_stops_when_planner_repeats_same_skill_beyond_cap(monkeypatch):
    """Planner keeps emitting 'run repo-fault-localize ...'; with cap=2 the loop
    must stop after the 2nd use instead of running it a 3rd/4th time.
    """
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.SEQUENCING_MODE", "replan", raising=False
    )
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.REPLAN_COMPOUND_GATE", False, raising=False
    )
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.REPLAN_MAX_SKILL_REPEATS", 2, raising=False
    )
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.MAX_SEQ_STEPS", 6, raising=False
    )
    orch, runner, skill_router = _make_orch_with_runner()

    run_calls = {"n": 0}

    async def _fake_run(subgoal):
        run_calls["n"] += 1
        return {"ok": True, "result": "located", "skill_name": "repo-fault-localize"}

    skill_router.run = AsyncMock(side_effect=_fake_run)

    # Planner ALWAYS asks to run repo-fault-localize again (never declares done).
    same = {"done": False, "next": "Run repo-fault-localize on the module", "reason": ""}
    side = [_planner_msg(same) for _ in range(6)] + [_planner_msg({"summary": "done"})]

    async def _run():
        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock, side_effect=side,
        ):
            return await orch.react_execute("Find and fix all faults in the module")

    result = run_async(_run())
    # Guard caps skill reuse at 2 -> skill_router.run invoked at most twice,
    # NOT 4x (the live-A/B bug) and NOT MAX_SEQ_STEPS (6) times.
    assert run_calls["n"] <= 2
    assert isinstance(result, dict) and "summary" in result


def test_replan_stops_on_duplicate_subgoal(monkeypatch):
    """Planner emits the SAME sub-goal twice in a row -> loop finishes, does not
    run the duplicate a second time."""
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.SEQUENCING_MODE", "replan", raising=False
    )
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.REPLAN_COMPOUND_GATE", False, raising=False
    )
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.MAX_SEQ_STEPS", 6, raising=False
    )
    orch, runner, skill_router = _make_orch_with_runner()
    run_calls = {"n": 0}

    async def _fake_run(subgoal):
        run_calls["n"] += 1
        return {"ok": True, "result": "x", "skill_name": ""}  # no skill name -> dup path, not cap

    skill_router.run = AsyncMock(side_effect=_fake_run)
    dup = {"done": False, "next": "Review the module for bugs", "reason": ""}
    side = [_planner_msg(dup) for _ in range(6)] + [_planner_msg({"summary": "done"})]

    async def _run():
        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock, side_effect=side,
        ):
            return await orch.react_execute("Review then review the module")

    result = run_async(_run())
    # First sub-goal runs once; the immediate repeat trips duplicate_subgoal -> stop.
    assert run_calls["n"] == 1
    assert isinstance(result, dict)


def test_skill_first_mode_never_calls_replan_loop(monkeypatch):
    """In skill_first mode, _replan_loop must NOT be invoked (the per-sub-step
    reset/guard changes are inert for the default mode)."""
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.SEQUENCING_MODE", "skill_first", raising=False
    )
    orch, runner, skill_router = _make_orch_with_runner()
    skill_router.run = AsyncMock(return_value={"ok": True, "result": "done", "skill_name": "test-gen"})

    called = {"replan": 0}

    async def _spy(self_goal):
        called["replan"] += 1
        return {"ok": True, "summary": "", "tools_used": []}

    async def _run():
        with patch.object(AsyncOrchestrator, "_replan_loop", autospec=True, side_effect=lambda self, g: _spy(g)):
            return await orch.react_execute("review this file for bugs")

    run_async(_run())
    assert called["replan"] == 0


def test_react_mode_never_calls_replan_loop(monkeypatch):
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.SEQUENCING_MODE", "react", raising=False
    )
    orch, runner, skill_router = _make_orch_with_runner()
    called = {"replan": 0}

    async def _spy(self_goal):
        called["replan"] += 1
        return {"ok": True, "summary": "", "tools_used": []}

    # react mode goes straight to _run_react_loop; stub it so no model call is needed.
    async def _fake_loop(goal, max_steps):
        return {"ok": True, "summary": "done", "tools_used": []}

    async def _run():
        with patch.object(AsyncOrchestrator, "_replan_loop", autospec=True, side_effect=lambda self, g: _spy(g)), \
             patch.object(AsyncOrchestrator, "_run_react_loop", autospec=True, side_effect=lambda self, g, m: _fake_loop(g, m)):
            return await orch.react_execute("anything")

    run_async(_run())
    assert called["replan"] == 0


def test_single_substep_reset_does_not_reload_loaded_skill(monkeypatch):
    """The per-sub-step reset clears the activation COUNTER but preserves the
    activation CACHE (runner.loaded). Resetting between sub-steps must not cause
    runaway re-loading WITHIN a single sub-step: load_skill of an already-loaded
    skill returns 'already_loaded' and does not re-read the body.

    Uses a REAL SkillRunner to prove reset_activations() does not clear the cache.
    """
    from pathlib import Path
    from services.skill_runner.skill_runner import SkillRunner

    # Minimal on-disk skill so load_skill has a real body to load.
    import tempfile, os as _os
    d = tempfile.mkdtemp()
    sk = Path(d) / "demo"
    sk.mkdir()
    (sk / "SKILL.md").write_text("---\nname: demo\ndescription: demo skill\n---\nbody\n")
    runner = SkillRunner([Path(d)], max_chain=8)
    runner.discover()

    first = runner.load_skill("demo")
    assert first["response"]["status"] == "loaded"
    runner.reset_activations()  # simulate the per-sub-step reset
    second = runner.load_skill("demo")
    # Cache preserved -> already_loaded, NOT a fresh "loaded" body re-read.
    assert second["response"]["status"] == "already_loaded"
