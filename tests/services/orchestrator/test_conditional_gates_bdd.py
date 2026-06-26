"""pytest-bdd step defs for conditional_gates.feature.

Binds the Gherkin to the real classifier and the real graph routers. Mocked only;
no GPU, no services. The ambiguity gate's "still runs" assertions drive the real
assess_ambiguity node with a mocked architect() (no LLM)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_bdd import scenarios, given, when, then, parsers

from langgraph.graph import END
from services.orchestrator import events
from services.orchestrator.task_complexity import classify_complexity
from services.orchestrator.graph import make_nodes, ambiguity_router, verify_router
from services.orchestrator.types import create_goal
from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator
from tests.conftest import run_async

scenarios("features/conditional_gates.feature")


@pytest.fixture
def ctx():
    """Mutable scenario context."""
    return {"enabled": True, "task": "", "complexity": None, "assess_delta": None}


class _FakeEmitter:
    async def emit(self, type: str, **fields):
        pass


@pytest.fixture(autouse=True)
def _silence_events():
    token = events.current_emitter.set(_FakeEmitter())
    yield
    events.current_emitter.reset(token)


# ── Given ──────────────────────────────────────────────────────────────────

@given("the conditional-gates feature is enabled")
def _enabled(ctx):
    ctx["enabled"] = True


@given("the conditional-gates feature is disabled")
def _disabled(ctx):
    ctx["enabled"] = False


@given(parsers.parse('a task "{task}"'))
def _task(ctx, task):
    ctx["task"] = task


# ── When ───────────────────────────────────────────────────────────────────

@when("the orchestrator classifies the task complexity")
def _classify(ctx):
    ctx["complexity"] = classify_complexity(ctx["task"], enabled=ctx["enabled"])


# ── Then: classifier verdict ───────────────────────────────────────────────

@then(parsers.parse("the classifier marks skip_ambiguity {flag:w}"))
def _check_skip_ambiguity(ctx, flag):
    assert ctx["complexity"].skip_ambiguity is (flag == "true")


@then(parsers.parse("the classifier marks skip_verify {flag:w}"))
def _check_skip_verify(ctx, flag):
    assert ctx["complexity"].skip_verify is (flag == "true")


# ── Then: router behavior ──────────────────────────────────────────────────

@then("the ambiguity gate is skipped so routing goes straight to plan")
def _ambiguity_skipped(ctx):
    state = {"skip_ambiguity": ctx["complexity"].skip_ambiguity, "ambiguity": 0.9}
    assert ambiguity_router(state) == "plan"


@then("the verify gate is skipped so routing goes straight to check")
def _verify_skipped(ctx):
    state = {"skip_verify": ctx["complexity"].skip_verify, "_verify_reflect": True}
    assert verify_router(state) == "check"


@then("the ambiguity gate still runs and produces an ambiguity score")
def _ambiguity_runs(ctx):
    # Drive the real assess_ambiguity node with a mocked architect() (no LLM).
    # The task is non-trivial / disabled, so skip_ambiguity is False and the node
    # makes its architect() call and returns an ambiguity score.
    mock_orch = MagicMock(spec=CodingOrchestrator)
    mock_orch.architect = AsyncMock(
        return_value='{"assumptions": [], "ambiguity": 0.5, "blocking_question": ""}'
    )
    mock_async = MagicMock(spec=AsyncOrchestrator)
    assess = make_nodes(mock_orch, mock_async)[5]

    tree: dict = {}
    create_goal(tree, "root", None, ctx["task"])
    state = {
        "session_id": "bdd",
        "goal_tree": tree,
        "current_goal_id": "root",
        "step_markers": {},
        "messages": [],
        "error": None,
        "root_goal": ctx["task"],
    }

    delta = run_async(assess(state))
    mock_orch.architect.assert_awaited_once()
    assert "ambiguity" in delta
    assert delta["skip_ambiguity"] is False


@then("the verify gate still runs and routes a low-scoring artifact to reflect")
def _verify_runs(ctx):
    # skip_verify is False, so a below-threshold score reflects (normal behavior).
    state = {"skip_verify": ctx["complexity"].skip_verify, "_verify_reflect": True}
    assert verify_router(state) == "reflect"


# F2 FIX: Conditional gates skip_ambiguity cross-plan consistency
@pytest.mark.mocked
def test_skip_ambiguity_blocks_clarification_even_if_high_ambiguity_score(monkeypatch):
    """F2 FIX: trivial task with skip_ambiguity=True must NOT halt for
    clarification even if the LLM scores it >=AMBIGUITY_THRESHOLD.

    The assess_ambiguity node gates the awaiting_clarification block on
    `not complexity.skip_ambiguity`, so clarification is suppressed for
    trivial tasks. The clarification_router then sees awaiting_clarification=False
    and returns 'execute', not END.
    """
    # Enable conditional gates feature
    monkeypatch.setenv("ENABLE_CONDITIONAL_GATES", "1")

    # Trivial task that the classifier marks skip_ambiguity=True
    task = "What is 2+2?"
    complexity = classify_complexity(task, enabled=True)
    assert complexity.skip_ambiguity is True

    # But mock the architect() to return a HIGH ambiguity score (>=0.6)
    # to simulate the case where the classifier is conservative but the
    # LLM still finds the task ambiguous.
    mock_orch = MagicMock(spec=CodingOrchestrator)
    mock_orch.architect = AsyncMock(
        return_value='{"assumptions": ["user wants arithmetic"], "ambiguity": 0.8, "blocking_question": "Do you want decimal or binary?"}'
    )
    mock_async = MagicMock(spec=AsyncOrchestrator)
    assess = make_nodes(mock_orch, mock_async)[5]

    tree: dict = {}
    create_goal(tree, "root", None, task)
    state = {
        "session_id": "test_f2",
        "goal_tree": tree,
        "current_goal_id": "root",
        "step_markers": {},
        "messages": [],
        "error": None,
        "root_goal": task,
    }

    # Run assess_ambiguity node
    delta = run_async(assess(state))

    # The LLM did score it high (0.8), but assess_ambiguity gated the
    # clarification block on `not complexity.skip_ambiguity`, so awaiting_clarification
    # must NOT be set (not present in delta, which means it defaults to False in routers).
    assert "awaiting_clarification" not in delta
    assert "clarification_question" not in delta
    assert delta["ambiguity"] == 0.8  # still recorded for metrics
    assert delta["skip_ambiguity"] is True  # classifier verdict preserved
