"""Regression test for FIX 3 follow-up: mid-chain sub-intent failure must be retried.

FIX 3 nests sub-intents as a dependency chain (root -> subN -> ... -> sub1, with the
FIRST sub-intent the deepest leaf and run first). A FAILED non-leaf-of-execution
sub-intent PERMANENTLY blocks all of its ancestors at PENDING: every goal upstream of
it can never become ready (its child never COMPLETED). So check_node's all_terminal
guard was never satisfied, check returned {} early, NEVER reaching the failed_retryable
branch, current_goal_id stayed 'root' (PENDING), and router() returned END.

Net effect of the bug: the task silently terminated with NO final_answer and NO error,
and the failed sub-intent was never sent through reflect/retry.

These tests drive the REAL plan/execute/check/reflect nodes through the REAL router so
they reproduce the end-to-end control flow, and assert:
  1. a mid-chain failure reaches the `reflect` branch (router returns "reflect"), and
  2. after the retry succeeds, the task finalizes with a non-empty final_answer.
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
        "messages": [],
    }


def _build_nodes(monkeypatch, route_result, dispatch_fn):
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
    mock_orch.architect = AsyncMock(return_value="diagnosis: retry it")
    mock_orch.skill_router = fake_router

    mock_async_orch = MagicMock(spec=AsyncOrchestrator)
    mock_async_orch.plan_and_dispatch = AsyncMock(side_effect=dispatch_fn)

    plan_node, execute_node, check_node, reflect_node, *_ = graph_mod.make_nodes(
        mock_orch, mock_async_orch
    )
    return graph_mod, plan_node, execute_node, check_node, reflect_node


async def _drive(graph_mod, plan_node, execute_node, check_node, reflect_node, state):
    """Run the real plan -> (execute -> verify-skipped -> check -> router) loop.

    Mirrors the compiled graph but inlines the verify step (verify only critiques
    the LAST artifact and never routes to reflect for a 1.0 default score, so it is
    transparent here). Returns (final_state, router_decisions).
    """
    from services.orchestrator.types import State  # noqa: F401

    router_decisions: list[str] = []

    plan_out = await plan_node(state)
    state = {**state, **plan_out}

    # Bound the loop generously; a correct chain of 3 with one retry needs ~5 execs.
    for _ in range(40):
        exec_out = await execute_node(state)
        state = {**state, **exec_out}

        check_out = await check_node(state)
        # Merge check output (current_goal_id / final_answer / goal_tree).
        state = {**state, **check_out}

        decision = graph_mod.router(state)
        router_decisions.append(decision)

        if decision == graph_mod.END:
            break
        if decision == "reflect":
            reflect_out = await reflect_node(state)
            state = {**state, **reflect_out}
            # reflect routes back to execute; loop continues.
            continue
        # decision == "execute": loop continues to next execute super-step.

    return state, router_decisions


@pytest.mark.asyncio
async def test_midchain_failure_is_retried_and_finalizes(monkeypatch):
    """s2 fails once mid-chain; the graph must retry it (reflect) and then finalize."""
    from services.orchestrator.skill_router import RouteResult
    from services.orchestrator.coding_orchestrator import Result

    route_result = RouteResult(
        skills=["skill-a", "skill-b", "skill-c"],
        needs_clarification=False,
        sub_intents=["s1", "s2", "s3"],
    )

    # s2 fails on its FIRST dispatch, succeeds on its SECOND. Everything else passes.
    s2_attempts = {"n": 0}

    async def dispatch_fn(ready_goals):
        results = []
        for g in ready_goals:
            if g["description"] == "s2":
                s2_attempts["n"] += 1
                if s2_attempts["n"] == 1:
                    results.append(Result(id=g["id"], summary="boom: s2 failed", ok=False))
                    continue
            results.append(Result(id=g["id"], summary=f"done: {g['description']}", ok=True))
        return results

    graph_mod, plan_node, execute_node, check_node, reflect_node = _build_nodes(
        monkeypatch, route_result, dispatch_fn
    )

    state, decisions = await _drive(
        graph_mod, plan_node, execute_node, check_node, reflect_node,
        _make_state("do s1, then s2, then s3"),
    )

    # The failed mid-chain sub-intent MUST have reached the reflect branch
    # (the whole point: it is retried, not silently dropped).
    assert "reflect" in decisions, f"mid-chain failure never reached reflect: {decisions}"

    # s2 was dispatched twice (failed once, retried once).
    assert s2_attempts["n"] == 2, f"s2 should be retried exactly once, got {s2_attempts['n']}"

    # The task finalizes with a real answer — NOT a silent END with no final_answer.
    assert state.get("final_answer"), "task ended with no final_answer (the regression)"
    assert decisions[-1] == graph_mod.END

    # All three sub-intents completed (the chain ran to completion after the retry).
    descs_to_status = {
        g["description"]: g["status"]
        for g in state["goal_tree"].values()
        if g["id"] != "root"
    }
    assert descs_to_status == {
        "s1": "COMPLETED", "s2": "COMPLETED", "s3": "COMPLETED"
    }, descs_to_status
    # The final answer should mention each completed sub-intent's result.
    for d in ("s1", "s2", "s3"):
        assert f"done: {d}" in state["final_answer"], (d, state["final_answer"])


@pytest.mark.asyncio
async def test_check_routes_to_reflect_on_midchain_failure_before_all_terminal(monkeypatch):
    """Unit-level: check_node must surface a retryable failed descendant even though
    upstream descendants are still PENDING (not all_terminal)."""
    from services.orchestrator.skill_router import RouteResult
    from services.orchestrator.coding_orchestrator import Result

    route_result = RouteResult(
        skills=["skill-a", "skill-b", "skill-c"],
        needs_clarification=False,
        sub_intents=["s1", "s2", "s3"],
    )

    # Dispatch sequence: first execute runs the leaf s1 (ok), second runs s2 (FAIL).
    async def dispatch_fn(ready_goals):
        out = []
        for g in ready_goals:
            ok = g["description"] != "s2"
            summary = f"done: {g['description']}" if ok else "boom"
            out.append(Result(id=g["id"], summary=summary, ok=ok))
        return out

    graph_mod, plan_node, execute_node, check_node, reflect_node = _build_nodes(
        monkeypatch, route_result, dispatch_fn
    )

    state = _make_state("s1 then s2 then s3")
    plan_out = await plan_node(state)
    state = {**state, **plan_out}

    # Super-step 1: s1 (leaf) completes.
    state = {**state, **(await execute_node(state))}
    # Super-step 2: s2 becomes ready (its only child s1 is COMPLETED) and FAILS.
    state = {**state, **(await execute_node(state))}

    # At this point s3 and root are still PENDING (blocked behind FAILED s2):
    # this is the "not all_terminal" condition that used to make check return {}.
    statuses = {g["description"]: g["status"] for g in state["goal_tree"].values()}
    assert statuses["s2"] == "FAILED"
    assert statuses["s3"] == "PENDING"
    assert statuses["s1"] == "COMPLETED"

    check_out = await check_node(state)
    # check_node must NOT silently return {} — it must point current_goal_id at the
    # failed retryable descendant and set up the reflect route.
    assert check_out, "check_node returned empty on a mid-chain failure (the regression)"
    state = {**state, **check_out}
    failed_gid = state["current_goal_id"]
    assert state["goal_tree"][failed_gid]["description"] == "s2"
    assert state["goal_tree"][failed_gid]["status"] == "FAILED"

    # And the real router agrees: this routes to reflect, not END.
    assert graph_mod.router(state) == "reflect"
    # No final_answer yet — the task is not done, it is being retried.
    assert not state.get("final_answer")
