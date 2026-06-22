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


@pytest.mark.asyncio
async def test_plan_emits_clarification_request_when_needed(monkeypatch):
    """When route() needs clarification, plan emits clarification_request and sets the flag."""
    from services.orchestrator import graph as graph_mod
    from services.orchestrator.skill_router import RouteResult
    from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

    captured: list[tuple[str, dict]] = []

    async def fake_emit(type, **fields):
        captured.append((type, fields))

    # Patch the events module before importing make_nodes
    monkeypatch.setattr(graph_mod.events, "emit", fake_emit)

    route_result = RouteResult(
        skills=[],
        needs_clarification=True,
        clarification_question="Search or generate?",
        sub_intents=["search", "generate"],
    )
    fake_router = MagicMock()
    fake_router.route = AsyncMock(return_value=route_result)
    fake_router.runner.catalog_prompt.return_value = "CATALOG"

    mock_orch = MagicMock(spec=CodingOrchestrator)
    mock_orch.architect = AsyncMock(return_value="")  # Empty so fallback produces no children
    mock_orch.skill_router = fake_router
    mock_async_orch = MagicMock(spec=AsyncOrchestrator)

    plan_node, *_ = graph_mod.make_nodes(mock_orch, mock_async_orch)

    state = {
        "session_id": "s1",
        "goal_tree": {
            "root": {
                "id": "root", "parent_id": None, "children": [],
                "description": "search a dataset and generate examples",
                "status": "PENDING", "result": None, "error": None,
                "attempts": 0, "started_at": None, "updated_at": None,
            }
        },
        "current_goal_id": "root",
    }

    out = await plan_node(state)

    assert out.get("awaiting_clarification") is True
    assert out.get("clarification_question") == "Search or generate?"
    types_emitted = [t for t, _ in captured]
    assert "clarification_request" in types_emitted
    clar = next(f for t, f in captured if t == "clarification_request")
    assert clar["question"] == "Search or generate?"
    assert clar["task"] == "search a dataset and generate examples"
    assert clar["session_id"] == "s1"


@pytest.mark.asyncio
async def test_plan_expands_skills_into_sequential_child_goals(monkeypatch):
    """When route() returns skills, plan adds one child Goal per skill, chained."""
    from services.orchestrator import graph as graph_mod
    from services.orchestrator.skill_router import RouteResult
    from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

    async def fake_emit(type, **fields):
        pass

    monkeypatch.setattr(graph_mod.events, "emit", fake_emit)

    route_result = RouteResult(
        skills=["dataset-search", "synthetic-gen"],
        needs_clarification=False,
        sub_intents=["search a dataset", "generate examples"],
    )
    fake_router = MagicMock()
    fake_router.route = AsyncMock(return_value=route_result)
    fake_router.runner.catalog_prompt.return_value = "CATALOG"

    mock_orch = MagicMock(spec=CodingOrchestrator)
    mock_orch.architect = AsyncMock(return_value="Task 1\nTask 2")
    mock_orch.skill_router = fake_router
    mock_async_orch = MagicMock(spec=AsyncOrchestrator)

    plan_node, *_ = graph_mod.make_nodes(mock_orch, mock_async_orch)

    state = {
        "session_id": "s1",
        "goal_tree": {
            "root": {
                "id": "root", "parent_id": None, "children": [],
                "description": "search a dataset and generate examples",
                "status": "PENDING", "result": None, "error": None,
                "attempts": 0, "started_at": None, "updated_at": None,
            }
        },
        "current_goal_id": "root",
    }

    out = await plan_node(state)

    assert out.get("awaiting_clarification") in (False, None)
    tree = out["goal_tree"]
    children = tree["root"]["children"]
    assert len(children) == 2
    descs = [tree[c]["description"] for c in children]
    assert descs == ["search a dataset", "generate examples"]
    # Sequential: second child depends on the first (encoded as the first being a
    # child/parent of the second). Verify the chain via parent_id linkage.
    first, second = children
    assert tree[second]["parent_id"] == first
