"""Regression coverage for the multi-intent routing fixes — NEW file only.

This file exists to close a specific REGRESSION flagged in project review: two
EXISTING tracked test files (test_graph.py and test_graph_multi_intent.py) had
been edited in place to chase the new routing behavior, violating the
"new tests in NEW files only" constraint. Those in-place rewrites also gave a
FALSE sense of safety: they asserted only the goal-tree STRUCTURE and never
asserted (a) that the graph halts on clarification WITHOUT guessing a
final_answer, nor (b) the actual EXECUTION ORDER / final_answer CONTENT of a
sequential multi-intent route.

The obsolete in-place assertions have been removed from those two files (the
only unique still-valid coverage there — SkillRouter.runner property access — is
re-homed here in `test_skill_router_runner_property_is_accessible`). This file
provides the behavioral guarantees end-to-end so coverage is not lost.

All tests are mocked (no GPU / no services).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Re-homed unit coverage: SkillRouter.runner property (was the unique still-valid
# assertion inside the now-removed test_graph.py
# TestPlanNode.test_plan_node_with_real_skill_router_property_access).
# ---------------------------------------------------------------------------

@pytest.mark.mocked
def test_skill_router_runner_property_is_accessible():
    """The public SkillRouter.runner property (not private _runner) must expose
    the underlying SkillRunner. The plan node reads skill_router.runner.catalog_prompt()."""
    from services.orchestrator.skill_router import SkillRouter
    from services.skill_runner.skill_runner import SkillRunner, SkillMeta

    runner = MagicMock(spec=SkillRunner)
    runner.catalog = {
        "test-skill": SkillMeta(
            name="test-skill",
            description="Test skill",
            path=Path("/fake/SKILL.md"),
            tier="bundled",
        )
    }
    runner.catalog_prompt.return_value = "- test-skill: Test skill"

    skill_router = SkillRouter(
        runner=runner,
        redis=AsyncMock(),
        gemma_api_base="http://localhost:8000/v1",
    )

    assert skill_router.runner is runner
    assert skill_router.runner.catalog_prompt() == "- test-skill: Test skill"


# ---------------------------------------------------------------------------
# Helpers for end-to-end graph runs through the compiled StateGraph.
# ---------------------------------------------------------------------------

def _root_state(description: str, *, root_goal: str | None = None) -> dict:
    return {
        "session_id": "s1",
        "root_goal": root_goal if root_goal is not None else description,
        "goal_tree": {
            "root": {
                "id": "root", "parent_id": None, "children": [],
                "description": description,
                "status": "PENDING", "result": None, "error": None,
                "attempts": 0, "started_at": None, "updated_at": None,
            }
        },
        "current_goal_id": "root",
        "step_markers": {},
        "messages": [],
        "error": None,
        "final_answer": "",
    }


def _build_compiled_graph(monkeypatch, *, route_result, dispatch_recorder=None):
    """Compile the real graph with a MemorySaver and a fake skill router/orchestrator.

    If `dispatch_recorder` is given, every plan_and_dispatch call appends the
    ordered descriptions of the ready goals it received, so a caller can
    reconstruct the cross-super-step execution order. Each dispatched goal
    completes successfully with summary "done: <description>".
    """
    from services.orchestrator import graph as graph_mod
    from services.orchestrator.coding_orchestrator import (
        CodingOrchestrator,
        AsyncOrchestrator,
        Result,
    )
    from langgraph.checkpoint.memory import MemorySaver

    async def fake_emit(type, **fields):
        pass

    monkeypatch.setattr(graph_mod.events, "emit", fake_emit)

    fake_router = MagicMock()
    fake_router.route = AsyncMock(return_value=route_result)
    fake_router.runner.catalog_prompt.return_value = "CATALOG"

    mock_orch = MagicMock(spec=CodingOrchestrator)
    # assess_ambiguity calls architect; return low-ambiguity JSON so the graph
    # routes assess_ambiguity -> plan (not -> approval).
    mock_orch.architect = AsyncMock(
        return_value='{"assumptions": [], "ambiguity": 0.0, "blocking_question": ""}'
    )
    mock_orch.skill_router = fake_router

    async def fake_dispatch(ready_goals):
        if dispatch_recorder is not None:
            dispatch_recorder.append([g["description"] for g in ready_goals])
        return [
            Result(id=g["id"], summary=f"done: {g['description']}", ok=True)
            for g in ready_goals
        ]

    mock_async_orch = MagicMock(spec=AsyncOrchestrator)
    mock_async_orch.plan_and_dispatch = AsyncMock(side_effect=fake_dispatch)

    real_cp = MemorySaver()
    with patch("pymongo.MongoClient", return_value=MagicMock()):
        with patch("langgraph.checkpoint.mongodb.MongoDBSaver", return_value=real_cp):
            graph, _ = graph_mod.build_graph(mock_orch, mock_async_orch)
    return graph, mock_async_orch


# ---------------------------------------------------------------------------
# Regression 1 — clarification halts AND no guessed final_answer.
# The deleted in-place rewrites never asserted final_answer content; this does.
# ---------------------------------------------------------------------------

@pytest.mark.mocked
@pytest.mark.asyncio
async def test_clarification_halts_with_no_guessed_final_answer(monkeypatch):
    from services.orchestrator.skill_router import RouteResult

    route_result = RouteResult(
        skills=[],
        needs_clarification=True,
        clarification_question="Did you want to search or generate?",
        sub_intents=["search", "generate"],
    )
    graph, mock_async_orch = _build_compiled_graph(monkeypatch, route_result=route_result)

    final_state = await graph.ainvoke(
        _root_state("search a dataset and generate examples"),
        {"configurable": {"thread_id": "t-regress-clarify"}},
    )

    # Halted on clarification.
    assert final_state.get("awaiting_clarification") is True
    assert final_state.get("clarification_question") == "Did you want to search or generate?"
    # execute_node was never reached: no dispatch, no children, and crucially NO
    # guessed final_answer (the core promise: ask, don't guess).
    mock_async_orch.plan_and_dispatch.assert_not_called()
    assert not final_state.get("final_answer")
    assert final_state["goal_tree"]["root"]["children"] == []


# ---------------------------------------------------------------------------
# Regression 2 — a confident multi-intent route executes sub-intents in
# SUBMISSION ORDER and the final_answer CONTAINS every sub-intent's result, in
# that order. The deleted struct-only assertions checked neither.
# ---------------------------------------------------------------------------

@pytest.mark.mocked
@pytest.mark.asyncio
async def test_sequential_route_execution_order_and_final_answer_content(monkeypatch):
    from services.orchestrator.skill_router import RouteResult

    route_result = RouteResult(
        skills=["dataset-search", "dataset-generation", "results-analysis"],
        needs_clarification=False,
        sub_intents=["search a dataset", "generate examples", "analyze results"],
    )
    dispatched: list[list[str]] = []
    graph, mock_async_orch = _build_compiled_graph(
        monkeypatch, route_result=route_result, dispatch_recorder=dispatched
    )

    final_state = await graph.ainvoke(
        _root_state("search a dataset, generate examples, then analyze results"),
        {"configurable": {"thread_id": "t-regress-seq"}},
    )

    # No clarification was requested.
    assert not final_state.get("awaiting_clarification")

    # Execution order across super-steps == submission order, one goal per step.
    exec_order = [desc for batch in dispatched for desc in batch]
    assert exec_order == [
        "search a dataset",
        "generate examples",
        "analyze results",
    ], f"sub-intents must execute in submission order, got {exec_order}"
    assert all(len(batch) == 1 for batch in dispatched), dispatched

    # final_answer content: every sub-intent's result appears, and in submission order.
    final_answer = final_state.get("final_answer") or ""
    assert final_answer, "expected a finalized answer for a confident multi-intent route"
    positions = [final_answer.index(f"done: {d}") for d in exec_order if f"done: {d}" in final_answer]
    for d in exec_order:
        assert f"done: {d}" in final_answer, f"missing result for sub-intent {d!r} in final_answer"
    assert positions == sorted(positions), (
        f"final_answer must list sub-intent results in submission order; got order {positions}"
    )
