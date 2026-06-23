from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_state_has_clarification_fields():
    """The new TypedDict keys must be declared (annotations present)."""
    from services.orchestrator.types import State

    annotations = State.__annotations__
    assert "awaiting_clarification" in annotations
    assert "clarification_question" in annotations


def test_state_clarification_fields_optional():
    """State is total=False, so a State without the new keys is still valid at runtime."""
    from services.orchestrator.types import State

    s: State = {"session_id": "abc"}
    assert s["session_id"] == "abc"
    s["awaiting_clarification"] = True
    s["clarification_question"] = "Which?"
    assert s["awaiting_clarification"] is True
    assert s["clarification_question"] == "Which?"


def _import_plan():
    """Import the plan node. The graph module wires nodes via make_nodes/build_graph;
    grab the plan coroutine however the module exposes it. Adjust this helper to the
    real export (module-level `plan`, or via make_nodes) when implementing."""
    from services.orchestrator import graph as graph_mod
    return graph_mod


# NOTE: test_plan_emits_clarification_request_when_needed was removed — route() no
# longer clarifies (the assess_ambiguity gate owns ambiguity), so the plan node never
# emits clarification_request from a route() result. The multi-intent CHAIN test was
# also removed: single-intent routing creates AT MOST ONE child goal (see
# test_single_intent_removal.py for the new single-child contract).


@pytest.mark.asyncio
async def test_plan_creates_single_child_goal_for_skill_route(monkeypatch):
    """Single-intent routing: when route() returns a skill, plan creates exactly ONE
    child Goal (sub_intents[0]) wired under root, then proceeds to execute."""
    from services.orchestrator import graph as graph_mod
    from services.orchestrator.skill_router import RouteResult
    from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

    async def fake_emit(type, **fields):
        pass

    monkeypatch.setattr(graph_mod.events, "emit", fake_emit)

    route_result = RouteResult(
        skills=["dataset-search"],
        needs_clarification=False,
        sub_intents=["search a dataset"],
    )
    fake_router = MagicMock()
    fake_router.route = AsyncMock(return_value=route_result)
    fake_router.runner.catalog_prompt.return_value = "CATALOG"

    mock_orch = MagicMock(spec=CodingOrchestrator)
    mock_orch.architect = AsyncMock(return_value="Task 1")
    mock_orch.skill_router = fake_router
    mock_async_orch = MagicMock(spec=AsyncOrchestrator)

    plan_node, *_ = graph_mod.make_nodes(mock_orch, mock_async_orch)

    state = {
        "session_id": "s1",
        "goal_tree": {
            "root": {
                "id": "root", "parent_id": None, "children": [],
                "description": "search a dataset",
                "status": "PENDING", "result": None, "error": None,
                "attempts": 0, "started_at": None, "updated_at": None,
            }
        },
        "current_goal_id": "root",
    }

    out = await plan_node(state)

    assert out.get("awaiting_clarification") in (False, None)
    tree = out["goal_tree"]
    non_root = [gid for gid in tree if gid != "root"]
    assert len(non_root) == 1
    child_id = non_root[0]
    assert tree[child_id]["description"] == "search a dataset"
    assert tree[child_id]["parent_id"] == "root"
    assert tree["root"]["children"] == [child_id]
