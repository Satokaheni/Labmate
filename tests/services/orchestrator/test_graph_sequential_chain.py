"""FIX 3 — multi-intent routing must build a real sequential CHAIN of child goals.

Previously every expanded child was appended to root.children with children=[],
so get_ready_goals() (which keys off the children[] array, not parent_id) released
ALL of them in the first execute pass — they ran concurrently and the tree invariant
broke (parent_id said one thing, children[] another).

The fix nests the sub-intents in REVERSE so the FIRST sub-intent is the deepest
leaf (runs first) and the LAST sub-intent is root's direct child (runs last):
root -> sub3 -> sub2 -> sub1(leaf). get_ready_goals — which releases a goal only
once all its children are COMPLETED, i.e. the deepest leaf first — then executes
them one at a time in SUBMISSION ORDER (sub1, then sub2, then sub3).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_state():
    return {
        "session_id": "s1",
        "goal_tree": {
            "root": {
                "id": "root", "parent_id": None, "children": [],
                "description": "search a dataset, generate examples, then analyze",
                "status": "PENDING", "result": None, "error": None,
                "attempts": 0, "started_at": None, "updated_at": None,
            }
        },
        "current_goal_id": "root",
    }


def _make_plan_node(monkeypatch, route_result):
    from services.orchestrator import graph as graph_mod
    from services.orchestrator.coding_orchestrator import (
        CodingOrchestrator,
        AsyncOrchestrator,
    )

    async def fake_emit(type, **fields):
        pass

    monkeypatch.setattr(graph_mod.events, "emit", fake_emit)

    fake_router = MagicMock()
    fake_router.route = AsyncMock(return_value=route_result)
    fake_router.runner.catalog_prompt.return_value = "CATALOG"

    mock_orch = MagicMock(spec=CodingOrchestrator)
    mock_orch.architect = AsyncMock(return_value="")
    mock_orch.skill_router = fake_router
    mock_async_orch = MagicMock(spec=AsyncOrchestrator)

    plan_node, *_ = graph_mod.make_nodes(mock_orch, mock_async_orch)
    return plan_node


@pytest.mark.asyncio
async def test_three_skill_route_builds_sequential_chain(monkeypatch):
    """A 3-skill route produces a real dependency chain: only ONE goal is ready
    initially, and parent_id/children[] are mutually consistent (no invariant break)."""
    from services.orchestrator.skill_router import RouteResult
    from services.orchestrator.types import get_ready_goals, Status

    route_result = RouteResult(
        skills=["dataset-search", "dataset-generation", "results-analysis"],
        needs_clarification=False,
        sub_intents=["search a dataset", "generate examples", "analyze results"],
    )
    plan_node = _make_plan_node(monkeypatch, route_result)

    out = await plan_node(route_result and _make_state())
    tree = out["goal_tree"]

    # All four nodes present: root + 3 sub-intents.
    assert len(tree) == 4

    # Exactly ONE goal is ready in the first execute pass (the leaf of the chain).
    ready = get_ready_goals(tree)
    assert len(ready) == 1, f"expected exactly one ready goal, got {[g['id'] for g in ready]}"

    # The single ready goal is a leaf (no children) and PENDING.
    leaf = ready[0]
    assert leaf["children"] == []
    assert leaf["status"] == Status.PENDING.value

    # FIX 3: nesting is REVERSED so sub-intents execute in SUBMISSION ORDER.
    # root's direct child is the LAST sub-intent ("analyze results"); each earlier
    # sub-intent is nested deeper; the FIRST sub-intent ("search a dataset") is the
    # deepest leaf and is therefore released FIRST by get_ready_goals().
    root_children = tree["root"]["children"]
    assert len(root_children) == 1
    last_id = root_children[0]
    assert tree[last_id]["description"] == "analyze results"
    assert tree[last_id]["parent_id"] == "root"

    middle_id = tree[last_id]["children"][0]
    assert tree[middle_id]["description"] == "generate examples"
    assert tree[middle_id]["parent_id"] == last_id

    first_id = tree[middle_id]["children"][0]
    assert tree[first_id]["description"] == "search a dataset"
    assert tree[first_id]["parent_id"] == middle_id
    assert tree[first_id]["children"] == []

    # The leaf — released first — is the FIRST sub-intent ("search a dataset").
    assert leaf["id"] == first_id


@pytest.mark.asyncio
async def test_chain_invariant_parent_id_matches_children(monkeypatch):
    """parent_id and children[] never disagree: for every non-root node, it appears
    in exactly its parent's children[] list, and nowhere else."""
    from services.orchestrator.skill_router import RouteResult

    route_result = RouteResult(
        skills=["dataset-search", "dataset-generation"],
        needs_clarification=False,
        sub_intents=["search a dataset", "generate examples"],
    )
    plan_node = _make_plan_node(monkeypatch, route_result)

    out = await plan_node(_make_state())
    tree = out["goal_tree"]

    for gid, goal in tree.items():
        pid = goal["parent_id"]
        if pid is None:
            # root: appears in no parent's children list
            for other in tree.values():
                assert gid not in other["children"]
            continue
        # Non-root: parent exists and lists this node exactly once...
        assert pid in tree
        assert tree[pid]["children"].count(gid) == 1
        # ...and no OTHER node lists it.
        for ogid, other in tree.items():
            if ogid != pid:
                assert gid not in other["children"]
