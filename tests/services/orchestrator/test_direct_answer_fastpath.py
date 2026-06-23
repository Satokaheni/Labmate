"""FIX 10 (B) — direct-answer fast-path.

When route() reports a skill-less SINGLE intent (skills=[], needs_clarification=False,
len(sub_intents)==1), the plan node answers directly via architect() and HALTS the graph
instead of architect-decomposing + re-routing through execute/ReAct. These tests pin:
  - the plan node fast-path (direct_answer + final_answer, root COMPLETED, no subtasks),
  - the fast-path being suppressible via ENABLE_DIRECT_ANSWER_FASTPATH,
  - clarification_router halting on direct_answer / final_answer,
  - backward-compat (route_result None still architect-decomposes),
  - the skills path and needs_clarification path staying unchanged.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _state(desc: str = "What is 2+2?") -> dict:
    return {
        "session_id": "s1",
        "goal_tree": {
            "root": {
                "id": "root", "parent_id": None, "children": [],
                "description": desc,
                "status": "PENDING", "result": None, "error": None,
                "attempts": 0, "started_at": None, "updated_at": None,
            }
        },
        "current_goal_id": "root",
    }


def _make_orch(architect_return: str = "4"):
    from services.orchestrator.coding_orchestrator import CodingOrchestrator
    orch = MagicMock(spec=CodingOrchestrator)
    orch.architect = AsyncMock(return_value=architect_return)
    return orch


def _make_async_orch():
    from services.orchestrator.coding_orchestrator import AsyncOrchestrator
    return MagicMock(spec=AsyncOrchestrator)


@pytest.fixture(autouse=True)
def _silence_events(monkeypatch):
    from services.orchestrator import graph as graph_mod

    async def fake_emit(_type, **_fields):
        pass

    monkeypatch.setattr(graph_mod.events, "emit", fake_emit)


# ── plan node fast-path ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_plan_fastpath_answers_directly():
    from services.orchestrator import graph as graph_mod
    from services.orchestrator.skill_router import RouteResult

    route_result = RouteResult(skills=[], needs_clarification=False, sub_intents=["x"])
    fake_router = MagicMock()
    fake_router.route = AsyncMock(return_value=route_result)
    fake_router.runner.catalog_prompt.return_value = "CATALOG"

    orch = _make_orch(architect_return="The answer is 4.")
    orch.skill_router = fake_router

    plan_node, *_ = graph_mod.make_nodes(orch, _make_async_orch())
    out = await plan_node(_state())

    assert out.get("direct_answer") is True
    assert out.get("final_answer") == "The answer is 4."
    # architect() was called exactly once, with the goal description (the direct answer),
    # NOT the decompose prompt.
    orch.architect.assert_awaited_once()
    assert orch.architect.await_args.args[0] == "What is 2+2?"
    # Root is COMPLETED with the answer; NO decompose subtasks were created.
    tree = out["goal_tree"]
    assert tree["root"]["status"] == "COMPLETED"
    assert tree["root"]["result"] == "The answer is 4."
    assert tree["root"]["children"] == []
    assert len(tree) == 1  # only root


@pytest.mark.asyncio
async def test_plan_fastpath_uses_direct_answer_budget():
    from services.orchestrator import graph as graph_mod
    from services.orchestrator.skill_router import RouteResult

    route_result = RouteResult(skills=[], needs_clarification=False, sub_intents=["x"])
    fake_router = MagicMock()
    fake_router.route = AsyncMock(return_value=route_result)
    fake_router.runner.catalog_prompt.return_value = "CATALOG"

    orch = _make_orch()
    orch.skill_router = fake_router

    plan_node, *_ = graph_mod.make_nodes(orch, _make_async_orch())
    await plan_node(_state())

    assert orch.architect.await_args.kwargs["thinking_budget"] == graph_mod.DIRECT_ANSWER_THINKING_BUDGET


@pytest.mark.asyncio
async def test_plan_fastpath_disabled_falls_through_to_decompose(monkeypatch):
    """With ENABLE_DIRECT_ANSWER_FASTPATH false, the skill-less single intent falls
    through to the old architect-decompose behavior."""
    from services.orchestrator import graph as graph_mod
    from services.orchestrator.skill_router import RouteResult

    monkeypatch.setattr(graph_mod, "ENABLE_DIRECT_ANSWER_FASTPATH", False)

    route_result = RouteResult(skills=[], needs_clarification=False, sub_intents=["x"])
    fake_router = MagicMock()
    fake_router.route = AsyncMock(return_value=route_result)
    fake_router.runner.catalog_prompt.return_value = "CATALOG"

    orch = _make_orch(architect_return="Subtask A\nSubtask B")
    orch.skill_router = fake_router

    plan_node, *_ = graph_mod.make_nodes(orch, _make_async_orch())
    out = await plan_node(_state())

    # Old behavior: architect-decompose ran (children created), no direct_answer.
    assert out.get("direct_answer") in (None, False)
    assert "final_answer" not in out
    assert len(out["goal_tree"]["root"]["children"]) == 2
    # architect was called with the decompose prompt (contains the catalog).
    assert "CATALOG" in orch.architect.await_args.args[0]


# ── skills path unchanged ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_skills_path_unchanged():
    """A confident multi-intent (non-empty skills) still builds a child-goal chain
    and does NOT take the direct-answer fast-path."""
    from services.orchestrator import graph as graph_mod
    from services.orchestrator.skill_router import RouteResult

    route_result = RouteResult(
        skills=["dataset-search"],
        needs_clarification=False,
        sub_intents=["search a dataset"],
    )
    fake_router = MagicMock()
    fake_router.route = AsyncMock(return_value=route_result)
    fake_router.runner.catalog_prompt.return_value = "CATALOG"

    orch = _make_orch()
    orch.skill_router = fake_router

    plan_node, *_ = graph_mod.make_nodes(orch, _make_async_orch())
    out = await plan_node(_state("search a dataset"))

    assert out.get("direct_answer") in (None, False)
    assert "final_answer" not in out
    assert out.get("awaiting_clarification") is False
    # A child goal was created for the skill; architect was NOT used.
    assert len(out["goal_tree"]["root"]["children"]) == 1
    orch.architect.assert_not_called()


# NOTE: the former test_needs_clarification_path_unchanged was removed — route() no
# longer sets needs_clarification (the assess_ambiguity gate owns clarification), and
# the plan node no longer has a needs_clarification branch. A skill-less route_result now
# always takes the direct-answer fast-path (covered above).


# ── backward-compat: route_result None still decomposes ────────────────────

@pytest.mark.asyncio
async def test_route_none_still_architect_decomposes():
    from services.orchestrator import graph as graph_mod

    fake_router = MagicMock()
    fake_router.route = AsyncMock(side_effect=TypeError("no live LLM"))
    fake_router.runner.catalog_prompt.return_value = "CATALOG"

    orch = _make_orch(architect_return="Subtask A\nSubtask B")
    orch.skill_router = fake_router

    plan_node, *_ = graph_mod.make_nodes(orch, _make_async_orch())
    out = await plan_node(_state("do a clear task"))

    orch.architect.assert_awaited_once()
    assert out.get("direct_answer") in (None, False)
    assert "final_answer" not in out
    assert len(out["goal_tree"]["root"]["children"]) == 2


# ── clarification_router ───────────────────────────────────────────────────

def test_clarification_router_halts_on_direct_answer():
    from services.orchestrator.graph import clarification_router
    from langgraph.graph import END

    assert clarification_router({"direct_answer": True}) == END


def test_clarification_router_halts_on_final_answer():
    from services.orchestrator.graph import clarification_router
    from langgraph.graph import END

    assert clarification_router({"final_answer": "4"}) == END


def test_clarification_router_halts_on_awaiting_clarification():
    from services.orchestrator.graph import clarification_router
    from langgraph.graph import END

    assert clarification_router({"awaiting_clarification": True}) == END


def test_clarification_router_executes_otherwise():
    from services.orchestrator.graph import clarification_router

    assert clarification_router({}) == "execute"
    assert clarification_router({"awaiting_clarification": False, "final_answer": ""}) == "execute"
