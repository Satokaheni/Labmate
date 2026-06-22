"""FIX 3 (regression) — sequential sub-intents must EXECUTE in SUBMISSION ORDER.

The plan node builds a dependency chain of child goals from a confident multi-intent
route. get_ready_goals() releases a PENDING goal only once ALL of its children are
COMPLETED — i.e. the DEEPEST LEAF first. The original chaining made the LAST
sub-intent the leaf, so the chain executed BACKWARDS:

    submitted: ["search a dataset", "generate examples"]
    EXEC ORDER (buggy): ["generate examples", "search a dataset"]   <- reversed!

This defeats the purpose: for "search, THEN generate", search must precede generate.
These tests drive the goals end-to-end through the real execute_node (which calls
AsyncOrchestrator.plan_and_dispatch) and assert plan_and_dispatch receives the
sub-intents in SUBMISSION ORDER.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_state(description: str):
    return {
        "session_id": "s1",
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
    }


def _build_nodes(monkeypatch, route_result, dispatch_recorder):
    """Wire a plan_node + execute_node sharing one fake orchestrator.

    `dispatch_recorder` is a list; every plan_and_dispatch call appends the
    descriptions of the ready goals it was handed (in order), so the test can
    reconstruct the end-to-end execution order across super-steps.
    """
    from services.orchestrator import graph as graph_mod
    from services.orchestrator.coding_orchestrator import (
        CodingOrchestrator,
        AsyncOrchestrator,
        Result,
    )

    async def fake_emit(type, **fields):
        pass

    monkeypatch.setattr(graph_mod.events, "emit", fake_emit)

    fake_router = MagicMock()
    fake_router.route = AsyncMock(return_value=route_result)
    fake_router.runner.catalog_prompt.return_value = "CATALOG"

    async def fake_dispatch(ready_goals):
        # Record the descriptions handed to this super-step, in order.
        dispatch_recorder.append([g["description"] for g in ready_goals])
        # Every dispatched goal completes successfully.
        return [
            Result(id=g["id"], summary=f"done: {g['description']}", ok=True)
            for g in ready_goals
        ]

    mock_orch = MagicMock(spec=CodingOrchestrator)
    mock_orch.architect = AsyncMock(return_value="")
    mock_orch.skill_router = fake_router

    mock_async_orch = MagicMock(spec=AsyncOrchestrator)
    mock_async_orch.plan_and_dispatch = AsyncMock(side_effect=fake_dispatch)

    plan_node, execute_node, *_ = graph_mod.make_nodes(mock_orch, mock_async_orch)
    return plan_node, execute_node


async def _run_to_completion(plan_node, execute_node, state):
    """Drive plan once, then loop execute_node until no goals remain ready.

    Mirrors the compiled graph's execute -> verify -> check -> execute loop, but
    isolates the ordering behavior under test (each execute super-step processes
    exactly the goals get_ready_goals() releases for it).
    """
    from services.orchestrator.types import get_ready_goals

    plan_out = await plan_node(state)
    state = {**state, **plan_out}

    # Guard against an infinite loop if the chain were ever malformed.
    for _ in range(20):
        ready = get_ready_goals(state["goal_tree"])
        # The root goal becomes "ready" only once every sub-intent has completed;
        # in the real graph the `check` node finalizes it (it is never re-executed).
        # Exclude it here so the driver models only the execute loop.
        ready = [g for g in ready if g["id"] != "root"]
        if not ready:
            break
        exec_out = await execute_node(state)
        state = {**state, **exec_out}
    return state


@pytest.mark.asyncio
async def test_two_sub_intents_execute_in_submission_order(monkeypatch):
    from services.orchestrator.skill_router import RouteResult

    route_result = RouteResult(
        skills=["dataset-search", "dataset-generation"],
        needs_clarification=False,
        sub_intents=["search a dataset", "generate examples"],
    )
    dispatched: list[list[str]] = []
    plan_node, execute_node = _build_nodes(monkeypatch, route_result, dispatched)

    await _run_to_completion(
        plan_node, execute_node, _make_state("search a dataset, then generate examples")
    )

    # Flatten the per-super-step dispatch records into one execution order.
    exec_order = [desc for batch in dispatched for desc in batch]
    assert exec_order == ["search a dataset", "generate examples"], (
        f"sub-intents must execute in submission order, got {exec_order}"
    )
    # Each super-step releases exactly ONE goal (true sequential, not parallel).
    assert all(len(batch) == 1 for batch in dispatched), dispatched


@pytest.mark.asyncio
async def test_three_sub_intents_execute_in_submission_order(monkeypatch):
    from services.orchestrator.skill_router import RouteResult

    route_result = RouteResult(
        skills=["dataset-search", "dataset-generation", "results-analysis"],
        needs_clarification=False,
        sub_intents=["search a dataset", "generate examples", "analyze results"],
    )
    dispatched: list[list[str]] = []
    plan_node, execute_node = _build_nodes(monkeypatch, route_result, dispatched)

    await _run_to_completion(
        plan_node,
        execute_node,
        _make_state("search a dataset, generate examples, then analyze results"),
    )

    exec_order = [desc for batch in dispatched for desc in batch]
    assert exec_order == [
        "search a dataset",
        "generate examples",
        "analyze results",
    ], f"sub-intents must execute in submission order, got {exec_order}"
    assert all(len(batch) == 1 for batch in dispatched), dispatched


@pytest.mark.asyncio
async def test_first_ready_goal_is_first_sub_intent(monkeypatch):
    """The very first goal released for execution is the FIRST sub-intent (the leaf)."""
    from services.orchestrator.skill_router import RouteResult
    from services.orchestrator.types import get_ready_goals

    route_result = RouteResult(
        skills=["dataset-search", "dataset-generation"],
        needs_clarification=False,
        sub_intents=["search a dataset", "generate examples"],
    )
    dispatched: list[list[str]] = []
    plan_node, _execute = _build_nodes(monkeypatch, route_result, dispatched)

    plan_out = await plan_node(_make_state("search then generate"))
    ready = get_ready_goals(plan_out["goal_tree"])
    assert len(ready) == 1
    assert ready[0]["description"] == "search a dataset"
