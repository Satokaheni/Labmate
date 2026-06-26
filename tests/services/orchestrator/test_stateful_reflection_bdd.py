"""pytest-bdd step defs for stateful_reflection.feature."""
from __future__ import annotations

import pytest
from contextvars import copy_context
from unittest.mock import AsyncMock, MagicMock
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator import events
from services.orchestrator.graph import make_nodes, collect_prior_reflections
from services.orchestrator.coding_orchestrator import (
    CodingOrchestrator,
    AsyncOrchestrator,
)
from services.orchestrator.types import Status, create_goal
from tests.conftest import run_async

pytestmark = pytest.mark.mocked

scenarios("features/stateful_reflection.feature")


@pytest.fixture
def ctx():
    # Shared mutable context across Given/When/Then steps.
    return {
        "messages": [],
        "goal_id": "g1",
        "error": "AssertionError: expected 4 got 5",
        "attempts": 1,
        "captured_prompt": None,
        "node_output": None,
        "collected": None,
    }


def _build_reflect(architect_return="DIAGNOSIS-OUT"):
    mock_orch = MagicMock(spec=CodingOrchestrator)
    mock_orch.architect = AsyncMock(return_value=architect_return)
    mock_async_orch = MagicMock(spec=AsyncOrchestrator)
    nodes = make_nodes(mock_orch, mock_async_orch)
    reflect = nodes[3]  # plan, execute_node, check, reflect, ...
    return reflect, mock_orch


async def _run(node, state):
    emitter = MagicMock()
    emitter.emit = AsyncMock()

    async def _call():
        token = events._current_emitter.set(emitter) if hasattr(events, "_current_emitter") else None
        try:
            return await node(state)
        finally:
            if token is not None:
                events._current_emitter.reset(token)

    return await copy_context().run(lambda: _call())


def _state(ctx):
    tree = {}
    create_goal(tree, ctx["goal_id"], None, "Compute 2+2")
    tree[ctx["goal_id"]]["status"] = Status.FAILED.value
    tree[ctx["goal_id"]]["error"] = ctx["error"]
    tree[ctx["goal_id"]]["attempts"] = ctx["attempts"]
    return {
        "session_id": "s1",
        "current_goal_id": ctx["goal_id"],
        "goal_tree": tree,
        "messages": ctx["messages"],
    }


# ---- Given ---------------------------------------------------------------- #

@given(parsers.parse('a goal "{gid}" that has failed once with error "{error}"'))
def goal_failed_once(ctx, gid, error):
    ctx["goal_id"] = gid
    ctx["error"] = error
    ctx["attempts"] = 1


@given(parsers.parse('a goal "{gid}" that has failed twice with error "{error}"'))
def goal_failed_twice(ctx, gid, error):
    ctx["goal_id"] = gid
    ctx["error"] = error
    ctx["attempts"] = 2


@given(parsers.parse('the state has no prior reflections for goal "{gid}"'))
def no_prior_reflections(ctx, gid):
    ctx["messages"] = []


@given(parsers.parse('a prior reflection for goal "{gid}" saying "{text}"'))
def a_prior_reflection_saying(ctx, gid, text):
    ctx["messages"].append({"role": "reflection", "goal_id": gid, "content": text})


@given(parsers.parse('a prior reflection "{text}" for goal "{gid}"'))
def a_prior_reflection_for(ctx, text, gid):
    ctx["messages"].append({"role": "reflection", "goal_id": gid, "content": text})


@given(parsers.parse('a goal "{gid}" with 5 prior reflections numbered "{first}" through "{last}"'))
def five_prior_reflections(ctx, gid, first, last):
    ctx["goal_id"] = gid
    ctx["messages"] = [
        {"role": "reflection", "goal_id": gid, "content": f"fix {i}"}
        for i in range(1, 6)
    ]


# ---- When ----------------------------------------------------------------- #

@when("the reflect node runs", target_fixture="ran")
def reflect_runs(ctx):
    reflect, mock_orch = _build_reflect()
    out = run_async(_run(reflect, _state(ctx)))
    ctx["node_output"] = out
    ctx["captured_prompt"] = mock_orch.architect.call_args.args[0]
    return True


@when(parsers.parse('prior reflections are collected for goal "{gid}"'))
def collect_for_goal(ctx, gid):
    ctx["collected"] = collect_prior_reflections({"messages": ctx["messages"]}, gid)


# ---- Then ----------------------------------------------------------------- #

@then('the diagnosis prompt does not contain a "previously tried" section')
def prompt_no_prior_section(ctx):
    assert "Previously tried" not in ctx["captured_prompt"]


@then(parsers.parse('the diagnosis prompt contains "{text}"'))
def prompt_contains(ctx, text):
    assert text in ctx["captured_prompt"]


@then("the diagnosis prompt instructs the model not to repeat a previously-tried fix")
def prompt_forbids_repeat(ctx):
    assert "do NOT repeat" in ctx["captured_prompt"]


@then(parsers.parse('the emitted reflection message is tagged with goal_id "{gid}"'))
def message_tagged(ctx, gid):
    msgs = ctx["node_output"]["messages"]
    assert msgs[0]["role"] == "reflection"
    assert msgs[0]["goal_id"] == gid


@then(parsers.parse("exactly {n:d} reflections are returned"))
def exactly_n_returned(ctx, n):
    assert len(ctx["collected"]) == n


@then(parsers.parse('they are "{a}", "{b}", and "{c}" in that order'))
def three_in_order(ctx, a, b, c):
    assert ctx["collected"] == [a, b, c]


@then(parsers.parse('it is "{text}"'))
def single_value_is(ctx, text):
    assert ctx["collected"] == [text]
