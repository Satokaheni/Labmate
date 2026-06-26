"""Step definitions for error classification BDD contract.

Verifies that failed goals are correctly classified (terminal vs retryable)
and that the orchestrator's execute node respects the classification.

Consumes: classify_error, ErrorClass, is_terminal (from error_classifier.py)
          make_nodes, MAX_GOAL_ATTEMPTS (from graph.py)
          types module for state helpers
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.error_classifier import (
    ErrorClass,
    classify_error,
    is_terminal,
)
from services.orchestrator.types import (
    Status, create_goal, update_status,
)

pytestmark = [pytest.mark.bdd, pytest.mark.mocked]

# Bind every scenario in the feature file.
scenarios("features/error_classification.feature")


def _decision_for(cls: ErrorClass) -> str:
    """Map a class to the contract's decision token used in the Examples table."""
    if is_terminal(cls):
        return "terminal"
    if cls == ErrorClass.RATE_LIMITED:
        return "backoff"
    return "retry"  # TRANSIENT or RETRYABLE


def _fresh_state() -> dict:
    """Create a minimal fresh state for BDD tests."""
    tree: dict = {}
    create_goal(tree, "root", None, "top-level task")
    return {
        "session_id": "bdd-001",
        "goal_tree": tree,
        "current_goal_id": "root",
        "step_markers": {},
        "messages": [],
        "error": None,
        "root_goal": "top-level task",
        "last_artifact": {"type": "other", "payload": ""},
    }


# --- Outline scenario steps -------------------------------------------------

@pytest.fixture
def ctx() -> dict:
    """Per-scenario context holder for step data."""
    return {}


@given("the error classifier is available")
def classifier_available():
    assert classify_error is not None


@when(parsers.re('I classify the error "(?P<error>.*)"'))
def classify(ctx, error):
    # The Examples table renders an empty cell as the empty string.
    # Using re() instead of parse() to handle quotes and empty strings.
    ctx["error_result"] = classify_error(error)


@then(parsers.re('the error class is "(?P<error_class>.*)"'))
def assert_class(ctx, error_class):
    assert ctx["error_result"].value == error_class


@then(parsers.re('the retry decision is "(?P<decision>.*)"'))
def assert_decision(ctx, decision):
    assert _decision_for(ctx["error_result"]) == decision


# --- Graph scenario steps ---------------------------------------------------

@given(parsers.parse('a goal that fails with "{error}"'))
def goal_fails(ctx, error):
    from services.orchestrator.coding_orchestrator import (
        CodingOrchestrator, AsyncOrchestrator, Result,
    )
    mock_orch = MagicMock(spec=CodingOrchestrator)
    mock_async = MagicMock(spec=AsyncOrchestrator)
    mock_async.plan_and_dispatch = AsyncMock(
        return_value=[Result(id="root", summary=error, ok=False)]
    )
    ctx["orch"] = mock_orch
    ctx["async_orch"] = mock_async
    ctx["state"] = _fresh_state()
    ctx["attempts_before"] = ctx["state"]["goal_tree"]["root"].get("attempts", 0)


@when("the execute node processes the failed result")
def run_execute(ctx):
    import asyncio
    from services.orchestrator.graph import make_nodes
    _, execute_node, *_ = make_nodes(ctx["orch"], ctx["async_orch"])
    ctx["delta"] = asyncio.run(execute_node(ctx["state"]))


@then("the goal is marked exhausted at MAX_GOAL_ATTEMPTS")
def assert_exhausted(ctx):
    from services.orchestrator.graph import MAX_GOAL_ATTEMPTS
    assert ctx["delta"]["goal_tree"]["root"]["attempts"] == MAX_GOAL_ATTEMPTS


@then("the goal's attempts increments by one")
def assert_incremented(ctx):
    assert ctx["delta"]["goal_tree"]["root"]["attempts"] == ctx["attempts_before"] + 1


@then(parsers.parse('the goal\'s error_class is "{error_class}"'))
def assert_goal_error_class(ctx, error_class):
    assert ctx["delta"]["error_class"] == error_class
    assert ctx["delta"]["goal_tree"]["root"]["error_class"] == error_class
