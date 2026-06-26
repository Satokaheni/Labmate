"""Graph wire-in for conditional gates: assess node skip path + both routers.

Mocked only. Proves BOTH branches: trivial tasks skip the gates, non-trivial /
ambiguous tasks remain gated, and the master flag OFF preserves today's behavior.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.orchestrator import events
from services.orchestrator.types import create_goal


class _FakeEmitter:
    def __init__(self):
        self.events = []

    async def emit(self, type: str, **fields):
        self.events.append((type, fields))


@pytest.fixture
def fake_emitter():
    emitter = _FakeEmitter()
    token = events.current_emitter.set(emitter)
    yield emitter
    events.current_emitter.reset(token)


def _make_state(**overrides) -> dict:
    tree: dict = {}
    create_goal(tree, "root", None, "top-level task")
    base = {
        "session_id": "test-cg-001",
        "goal_tree": tree,
        "current_goal_id": "root",
        "step_markers": {},
        "messages": [],
        "error": None,
        "root_goal": "top-level task",
    }
    base.update(overrides)
    return base


def _nodes(architect_return: str):
    from services.orchestrator.graph import make_nodes
    from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

    mock_orch = MagicMock(spec=CodingOrchestrator)
    mock_orch.architect = AsyncMock(return_value=architect_return)
    mock_async_orch = MagicMock(spec=AsyncOrchestrator)
    return make_nodes(mock_orch, mock_async_orch), mock_orch


# ── assess_ambiguity skip path (feature ON) ────────────────────────────────

@pytest.mark.mocked
@pytest.mark.asyncio
async def test_assess_skips_llm_for_trivial_task(monkeypatch, fake_emitter):
    """With the feature ON and a trivial task, assess_ambiguity must NOT call the
    LLM and must commit skip flags so the routers short-circuit."""
    monkeypatch.setenv("ENABLE_CONDITIONAL_GATES", "1")
    nodes, mock_orch = _nodes('{"assumptions": [], "ambiguity": 0.0, "blocking_question": ""}')
    assess = nodes[5]

    delta = await assess(_make_state(root_goal="What is 2+2?"))

    # The expensive architect() ambiguity call was skipped entirely.
    mock_orch.architect.assert_not_called()
    assert delta["skip_ambiguity"] is True
    assert delta["skip_verify"] is True
    assert delta["complexity"]["reason"]
    # Not flagged ambiguous, so the graph proceeds to plan.
    assert delta.get("awaiting_clarification") is not True
    assert delta.get("ambiguity", 0.0) == 0.0


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_assess_still_runs_llm_for_ambiguous_task(monkeypatch, fake_emitter):
    """A vague task is NOT trivial, so assess_ambiguity still calls the LLM and gates."""
    monkeypatch.setenv("ENABLE_CONDITIONAL_GATES", "1")
    nodes, mock_orch = _nodes(
        '{"assumptions": ["?"], "ambiguity": 0.85, "blocking_question": "What should I make better?"}'
    )
    assess = nodes[5]

    delta = await assess(_make_state(root_goal="make it better"))

    mock_orch.architect.assert_awaited_once()
    assert delta["skip_ambiguity"] is False
    assert delta["ambiguity"] == 0.85
    assert delta["awaiting_clarification"] is True


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_assess_still_runs_llm_for_nontrivial_artifact_task(monkeypatch, fake_emitter):
    """An artifact-producing task keeps verify; assess still runs the LLM gate."""
    monkeypatch.setenv("ENABLE_CONDITIONAL_GATES", "1")
    nodes, mock_orch = _nodes('{"assumptions": [], "ambiguity": 0.1, "blocking_question": ""}')
    assess = nodes[5]

    delta = await assess(
        _make_state(root_goal="Implement a rate limiter with a sliding window and tests")
    )

    mock_orch.architect.assert_awaited_once()
    assert delta["skip_verify"] is False
    assert delta["skip_ambiguity"] is False


# ── feature OFF = identical to today ───────────────────────────────────────

@pytest.mark.mocked
@pytest.mark.asyncio
async def test_assess_flag_off_always_runs_llm(monkeypatch, fake_emitter):
    """Master flag OFF: even a trivial task runs the LLM gate (regression-safe)."""
    monkeypatch.delenv("ENABLE_CONDITIONAL_GATES", raising=False)
    nodes, mock_orch = _nodes('{"assumptions": [], "ambiguity": 0.0, "blocking_question": ""}')
    assess = nodes[5]

    delta = await assess(_make_state(root_goal="What is 2+2?"))

    mock_orch.architect.assert_awaited_once()
    assert delta["skip_ambiguity"] is False
    assert delta["skip_verify"] is False


# ── ambiguity_router honors skip flag ──────────────────────────────────────

@pytest.mark.mocked
def test_ambiguity_router_skips_to_plan_when_flagged():
    from services.orchestrator.graph import ambiguity_router
    # Even an above-threshold ambiguity yields "plan" when skip_ambiguity is set,
    # because the classifier already certified the task as trivial/clear.
    assert ambiguity_router(_make_state(skip_ambiguity=True, ambiguity=0.9)) == "plan"


@pytest.mark.mocked
def test_ambiguity_router_unaffected_when_flag_absent():
    from services.orchestrator.graph import ambiguity_router
    from langgraph.graph import END
    assert ambiguity_router(_make_state(ambiguity=0.7)) == END
    assert ambiguity_router(_make_state(ambiguity=0.2)) == "plan"


# ── verify_router honors skip flag ─────────────────────────────────────────

@pytest.mark.mocked
def test_verify_router_skips_to_check_when_flagged():
    from services.orchestrator.graph import verify_router
    # A below-threshold score would normally reflect; skip_verify forces check.
    assert verify_router(_make_state(skip_verify=True, critique_score=0.1, _verify_reflect=True)) == "check"


@pytest.mark.mocked
def test_verify_router_unaffected_when_flag_absent():
    from services.orchestrator.graph import verify_router
    assert verify_router(_make_state(_verify_reflect=True)) == "reflect"
    assert verify_router(_make_state(_verify_reflect=False)) == "check"
    assert verify_router(_make_state(critique_score=0.5)) == "reflect"
