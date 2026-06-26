"""Node-level tests for the stateful reflect node.

Drives the real reflect node with a mocked orchestrator (architect captured),
asserting the prompt is conditioned on prior reflections on retry and not on
the first attempt. No GPU / no live services.
"""
from __future__ import annotations

import pytest
from contextvars import copy_context
from unittest.mock import AsyncMock, MagicMock

from services.orchestrator import events
from services.orchestrator.graph import make_nodes
from services.orchestrator.coding_orchestrator import (
    CodingOrchestrator,
    AsyncOrchestrator,
)
from services.orchestrator.types import Status, create_goal


pytestmark = pytest.mark.mocked


def _make_reflect_node(architect_return="DIAGNOSIS-OUT"):
    mock_orch = MagicMock(spec=CodingOrchestrator)
    mock_orch.architect = AsyncMock(return_value=architect_return)
    mock_async_orch = MagicMock(spec=AsyncOrchestrator)
    # make_nodes returns: plan, execute_node, check, reflect, approval,
    # assess_ambiguity, verify  (7 nodes — see CLAUDE.md make_nodes arity note)
    plan, execute_node, check, reflect, approval, assess_ambiguity, verify = make_nodes(
        mock_orch, mock_async_orch
    )
    return reflect, mock_orch


def _state_with_failed_goal(gid="g1", error="AssertionError: expected 4 got 5",
                            attempts=1, messages=None):
    tree = {}
    create_goal(tree, gid, None, "Compute 2+2")
    tree[gid]["status"] = Status.FAILED.value
    tree[gid]["error"] = error
    tree[gid]["attempts"] = attempts
    return {
        "session_id": "s1",
        "current_goal_id": gid,
        "goal_tree": tree,
        "messages": messages or [],
    }


async def _run_node(node, state):
    # events uses a ContextVar emitter; run inside a fresh context with a noop emitter.
    noop = AsyncMock()
    emitter = MagicMock()
    emitter.emit = noop

    async def _call():
        token = events._current_emitter.set(emitter) if hasattr(events, "_current_emitter") else None
        try:
            return await node(state)
        finally:
            if token is not None:
                events._current_emitter.reset(token)

    return await copy_context().run(lambda: _call())


@pytest.mark.asyncio
async def test_first_attempt_prompt_has_no_prior_section():
    reflect, mock_orch = _make_reflect_node()
    state = _state_with_failed_goal(attempts=1, messages=[])
    await _run_node(reflect, state)

    prompt = mock_orch.architect.call_args.args[0]
    assert "Previously tried" not in prompt
    assert "do NOT repeat" not in prompt
    # Original behavior preserved: goal + error still present.
    assert "Compute 2+2" in prompt
    assert "AssertionError: expected 4 got 5" in prompt


@pytest.mark.asyncio
async def test_first_attempt_message_tagged_with_goal_id():
    reflect, mock_orch = _make_reflect_node(architect_return="fresh diagnosis")
    state = _state_with_failed_goal(gid="g1", attempts=1, messages=[])
    out = await _run_node(reflect, state)

    msgs = out["messages"]
    assert len(msgs) == 1
    assert msgs[0]["role"] == "reflection"
    assert msgs[0]["goal_id"] == "g1"
    assert msgs[0]["content"] == "fresh diagnosis"


@pytest.mark.asyncio
async def test_second_attempt_prompt_includes_prior_reflection_and_forbids_repeat():
    reflect, mock_orch = _make_reflect_node()
    prior = [
        {"role": "reflection", "goal_id": "g1",
         "content": "Try converting the input to int before adding"},
    ]
    state = _state_with_failed_goal(
        gid="g1", error="AssertionError: still wrong", attempts=2, messages=prior
    )
    await _run_node(reflect, state)

    prompt = mock_orch.architect.call_args.args[0]
    assert "Try converting the input to int before adding" in prompt
    assert "Previously tried" in prompt
    assert "do NOT repeat" in prompt


@pytest.mark.asyncio
async def test_second_attempt_excludes_other_goals_reflections():
    reflect, mock_orch = _make_reflect_node()
    msgs = [
        {"role": "reflection", "goal_id": "OTHER", "content": "irrelevant other-goal fix"},
        {"role": "reflection", "goal_id": "g1", "content": "relevant g1 fix"},
    ]
    state = _state_with_failed_goal(gid="g1", attempts=2, messages=msgs)
    await _run_node(reflect, state)

    prompt = mock_orch.architect.call_args.args[0]
    assert "relevant g1 fix" in prompt
    assert "irrelevant other-goal fix" not in prompt


@pytest.mark.asyncio
async def test_reflect_uses_reflect_thinking_budget():
    from services.orchestrator.graph import REFLECT_THINKING_BUDGET

    reflect, mock_orch = _make_reflect_node()
    state = _state_with_failed_goal(attempts=1, messages=[])
    await _run_node(reflect, state)

    assert mock_orch.architect.call_args.kwargs["thinking_budget"] == REFLECT_THINKING_BUDGET
