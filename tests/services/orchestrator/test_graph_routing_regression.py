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
    from services.skill_runner.skill_runner import SkillMeta, SkillRunner

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
        registry=AsyncMock(),
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
                "id": "root",
                "parent_id": None,
                "children": [],
                "description": description,
                "status": "PENDING",
                "result": None,
                "error": None,
                "attempts": 0,
                "started_at": None,
                "updated_at": None,
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
    from langgraph.checkpoint.memory import MemorySaver

    from services.orchestrator import graph as graph_mod
    from services.orchestrator.coding_orchestrator import (
        AsyncOrchestrator,
        CodingOrchestrator,
        Result,
    )

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
            Result(id=g["id"], summary=f"done: {g['description']}", ok=True) for g in ready_goals
        ]

    mock_async_orch = MagicMock(spec=AsyncOrchestrator)
    mock_async_orch.plan_and_dispatch = AsyncMock(side_effect=fake_dispatch)

    real_cp = MemorySaver()
    with patch("services.orchestrator.graph._make_sqlite_checkpointer", return_value=real_cp):
        graph, _ = graph_mod.build_graph(mock_orch, mock_async_orch)
    return graph, mock_async_orch


# ---------------------------------------------------------------------------
# Regression — a confident SINGLE-intent route executes its one child goal and
# the final_answer CONTAINS that result.
#
# NOTE: the previous multi-intent regressions (clarification-via-route() halt,
# and sequential-chain submission-order) were removed with the multi-intent
# decompose machinery. route() never clarifies now (assess_ambiguity owns that —
# see test_graph_clarification_halt.py) and there is at most ONE child goal.
# ---------------------------------------------------------------------------


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_single_intent_route_executes_and_finalizes(monkeypatch):
    from services.orchestrator.skill_router import RouteResult

    route_result = RouteResult(
        skills=["dataset-search"],
        needs_clarification=False,
        sub_intents=["search a dataset"],
    )
    dispatched: list[list[str]] = []
    graph, mock_async_orch = _build_compiled_graph(
        monkeypatch, route_result=route_result, dispatch_recorder=dispatched
    )

    final_state = await graph.ainvoke(
        _root_state("search a dataset"),
        {"configurable": {"thread_id": "t-regress-single"}},
    )

    # No clarification was requested.
    assert not final_state.get("awaiting_clarification")

    # Exactly one goal dispatched (single-intent => one child goal).
    exec_order = [desc for batch in dispatched for desc in batch]
    assert exec_order == ["search a dataset"], exec_order
    assert all(len(batch) == 1 for batch in dispatched), dispatched

    # final_answer contains the one sub-intent's result.
    final_answer = final_state.get("final_answer") or ""
    assert "done: search a dataset" in final_answer


# ---------------------------------------------------------------------------
# P2-B.1 regression — route() is POD-only, so a skill-less route_result must NOT
# take the direct-answer fast-path when the CLIENT hosts a documentation skill.
# Instead it routes to EXECUTE (the ReAct loop advertises load_skill there).
# ---------------------------------------------------------------------------


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_no_pod_skill_but_client_doc_skill_routes_to_execute(monkeypatch):
    """No pod skill matched, but a client doc-skill is present -> execute, not direct-answer."""
    from services.orchestrator import client_context
    from services.orchestrator.skill_router import RouteResult

    route_result = RouteResult(
        skills=[], needs_clarification=False, sub_intents=["write a greeting"]
    )
    dispatched: list[list[str]] = []
    graph, _ = _build_compiled_graph(
        monkeypatch, route_result=route_result, dispatch_recorder=dispatched
    )

    manifest = {
        "tools": [
            {"name": "read_file", "source": "builtin"},
            {
                "name": "repo-greeting",
                "source": "skill",
                "description": "house style for any greeting",
                "body": "MARKER body",
            },
        ]
    }
    token = client_context.set_manifest(manifest)
    try:
        final_state = await graph.ainvoke(
            _root_state("write a greeting"),
            {"configurable": {"thread_id": "t-docskill-exec"}},
        )
    finally:
        client_context.reset_manifest(token)

    # A child goal WAS dispatched (execute path) — the direct-answer fast-path was skipped.
    exec_order = [desc for batch in dispatched for desc in batch]
    assert exec_order == ["write a greeting"], exec_order
    assert not final_state.get("direct_answer")
    assert "done: write a greeting" in (final_state.get("final_answer") or "")


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_no_pod_skill_and_no_client_doc_skill_takes_direct_answer(monkeypatch):
    """Control: skill-less route with NO client doc-skills still takes the direct-answer
    fast-path (no child goal dispatched) — existing behavior is preserved."""
    from services.orchestrator import client_context
    from services.orchestrator.skill_router import RouteResult

    route_result = RouteResult(skills=[], needs_clarification=False, sub_intents=["what is 2+2"])
    dispatched: list[list[str]] = []
    graph, _ = _build_compiled_graph(
        monkeypatch, route_result=route_result, dispatch_recorder=dispatched
    )

    # No manifest (no client attached) -> client_doc_skills(None) == {} -> fast-path.
    token = client_context.set_manifest(None)
    try:
        final_state = await graph.ainvoke(
            _root_state("what is 2+2"),
            {"configurable": {"thread_id": "t-docskill-direct"}},
        )
    finally:
        client_context.reset_manifest(token)

    # NO child goal dispatched — the direct-answer fast-path fired.
    assert dispatched == [], dispatched
    assert final_state.get("direct_answer") is True
