"""A/B routing — plan node forwards State["routing_mode"] into route(mode=...).

When routing_mode is set on the State, the plan node must pass it through as the
mode kwarg to route(); when unset, it defaults to ROUTING_MODE (now "single").
NEW file.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_plan_node(monkeypatch, route_result):
    from services.orchestrator import graph as graph_mod
    from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

    async def fake_emit(type, **fields):
        pass

    monkeypatch.setattr(graph_mod.events, "emit", fake_emit)

    fake_router = MagicMock()
    fake_router.route = AsyncMock(return_value=route_result)
    fake_router.runner.catalog_prompt.return_value = "CATALOG"

    mock_orch = MagicMock(spec=CodingOrchestrator)
    mock_orch.architect = AsyncMock(return_value="answer")
    mock_orch.skill_router = fake_router
    mock_async_orch = MagicMock(spec=AsyncOrchestrator)

    plan_node, *_ = graph_mod.make_nodes(mock_orch, mock_async_orch)
    return plan_node, fake_router


def _state(routing_mode=None):
    state = {
        "session_id": "s1",
        "goal_tree": {
            "root": {
                "id": "root", "parent_id": None, "children": [],
                "description": "do a thing", "status": "PENDING",
                "result": None, "error": None, "attempts": 0,
                "started_at": None, "updated_at": None,
            }
        },
        "current_goal_id": "root",
    }
    if routing_mode is not None:
        state["routing_mode"] = routing_mode
    return state


@pytest.mark.asyncio
async def test_plan_forwards_single_mode(monkeypatch):
    from services.orchestrator.skill_router import RouteResult
    rr = RouteResult(skills=["dataset-search"], sub_intents=["do a thing"])
    plan_node, fake_router = _make_plan_node(monkeypatch, rr)

    await plan_node(_state(routing_mode="single"))

    assert fake_router.route.await_args.kwargs.get("mode") == "single"


@pytest.mark.asyncio
async def test_plan_defaults_mode_when_unset(monkeypatch):
    from services.orchestrator import graph as graph_mod
    from services.orchestrator.skill_router import RouteResult
    rr = RouteResult(skills=["dataset-search"], sub_intents=["do a thing"])
    plan_node, fake_router = _make_plan_node(monkeypatch, rr)

    await plan_node(_state(routing_mode=None))

    # Unset routing_mode falls back to ROUTING_MODE (default flipped to "single";
    # multi remains via explicit override). Assert against the live constant.
    assert fake_router.route.await_args.kwargs.get("mode") == graph_mod.ROUTING_MODE
    assert graph_mod.ROUTING_MODE == "single"


@pytest.mark.asyncio
async def test_plan_forwards_explicit_multi_mode(monkeypatch):
    # multi fallback: explicit routing_mode="multi" on State still routes multi.
    from services.orchestrator.skill_router import RouteResult
    rr = RouteResult(skills=["dataset-search"], sub_intents=["do a thing"])
    plan_node, fake_router = _make_plan_node(monkeypatch, rr)

    await plan_node(_state(routing_mode="multi"))

    assert fake_router.route.await_args.kwargs.get("mode") == "multi"
