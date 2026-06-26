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

    import asyncio

    delta = asyncio.get_event_loop().run_until_complete(assess(state))
    mock_orch.architect.assert_awaited_once()
    assert "ambiguity" in delta
    assert delta["skip_ambiguity"] is False


@then("the verify gate still runs and routes a low-scoring artifact to reflect")
def _verify_runs(ctx):
    # skip_verify is False, so a below-threshold score reflects (normal behavior).
    state = {"skip_verify": ctx["complexity"].skip_verify, "_verify_reflect": True}
    assert verify_router(state) == "reflect"
