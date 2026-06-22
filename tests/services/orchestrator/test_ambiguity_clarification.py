"""FIX 7: assess_ambiguity halts genuinely-ambiguous tasks for clarification.

NEW file only. Covers:
  1. assess_ambiguity on HIGH ambiguity sets awaiting_clarification + clarification_question
     and emits a clarification_request event.
  2. assess_ambiguity on LOW ambiguity returns without awaiting_clarification.
  3. ambiguity_router returns END when ambiguity >= threshold, "plan" when below.
  4. Compiled-graph integration: a high-ambiguity task halts at END WITHOUT reaching
     plan/execute (plan_and_dispatch never called) and final_state carries
     awaiting_clarification=True + the clarification_question.

All tests are mocked (no GPU / no services).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.orchestrator import events


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------

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
    from services.orchestrator.types import create_goal

    tree: dict = {}
    create_goal(tree, "root", None, "top-level task")
    base = {
        "session_id": "test-clar-001",
        "goal_tree": tree,
        "current_goal_id": "root",
        "step_markers": {},
        "messages": [],
        "error": None,
        "root_goal": "top-level task",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. High-ambiguity assess_ambiguity halts for clarification
# ---------------------------------------------------------------------------

@pytest.mark.mocked
@pytest.mark.asyncio
async def test_assess_ambiguity_high_halts_for_clarification(fake_emitter):
    from services.orchestrator.graph import make_nodes
    from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

    mock_orch = MagicMock(spec=CodingOrchestrator)
    mock_orch.architect = AsyncMock(return_value=(
        '{"assumptions": ["?"], "ambiguity": 0.8, "blocking_question": "What should I make better?"}'
    ))
    mock_async_orch = MagicMock(spec=AsyncOrchestrator)

    nodes = make_nodes(mock_orch, mock_async_orch)
    assess = nodes[5]

    delta = await assess(_make_state(root_goal="make it better"))

    assert delta["awaiting_clarification"] is True
    assert delta["clarification_question"] == "What should I make better?"
    assert delta["ambiguity"] == 0.8

    # A clarification_request event is emitted (same shape the plan node uses).
    clar = [(name, f) for name, f in fake_emitter.events if name == "clarification_request"]
    assert len(clar) == 1
    assert clar[0][1]["question"] == "What should I make better?"
    assert clar[0][1]["task"] == "make it better"
    assert clar[0][1]["session_id"] == "test-clar-001"

    # The assess_ambiguity score reasoning event still fires.
    nodes_emitted = [f.get("node") for name, f in fake_emitter.events if name == "reasoning"]
    assert "assess_ambiguity" in nodes_emitted
    # And no stale node='approval' reasoning event.
    assert "approval" not in nodes_emitted


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_assess_ambiguity_high_uses_generic_question_when_blank(fake_emitter):
    """When the model returns no blocking_question, a sensible generic fallback is used."""
    from services.orchestrator.graph import make_nodes
    from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

    mock_orch = MagicMock(spec=CodingOrchestrator)
    mock_orch.architect = AsyncMock(return_value=(
        '{"assumptions": [], "ambiguity": 0.9, "blocking_question": ""}'
    ))
    mock_async_orch = MagicMock(spec=AsyncOrchestrator)

    nodes = make_nodes(mock_orch, mock_async_orch)
    assess = nodes[5]

    delta = await assess(_make_state(root_goal="fix the thing"))

    assert delta["awaiting_clarification"] is True
    assert delta["clarification_question"]  # non-empty fallback
    clar = [f for name, f in fake_emitter.events if name == "clarification_request"]
    assert len(clar) == 1
    assert clar[0]["question"] == delta["clarification_question"]


# ---------------------------------------------------------------------------
# 2. Low-ambiguity assess_ambiguity does NOT halt
# ---------------------------------------------------------------------------

@pytest.mark.mocked
@pytest.mark.asyncio
async def test_assess_ambiguity_low_does_not_halt(fake_emitter):
    from services.orchestrator.graph import make_nodes
    from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

    mock_orch = MagicMock(spec=CodingOrchestrator)
    mock_orch.architect = AsyncMock(return_value=(
        '{"assumptions": [], "ambiguity": 0.1, "blocking_question": ""}'
    ))
    mock_async_orch = MagicMock(spec=AsyncOrchestrator)

    nodes = make_nodes(mock_orch, mock_async_orch)
    assess = nodes[5]

    delta = await assess(_make_state(root_goal="write a python function that reverses a string"))

    assert delta.get("awaiting_clarification") is not True
    assert "clarification_question" not in delta
    # No clarification_request event.
    assert not [name for name, _ in fake_emitter.events if name == "clarification_request"]


# ---------------------------------------------------------------------------
# 3. ambiguity_router
# ---------------------------------------------------------------------------

@pytest.mark.mocked
def test_ambiguity_router_returns_end_when_high():
    from services.orchestrator.graph import ambiguity_router
    from langgraph.graph import END

    assert ambiguity_router(_make_state(ambiguity=0.7)) == END
    assert ambiguity_router(_make_state(ambiguity=1.0)) == END


@pytest.mark.mocked
def test_ambiguity_router_returns_plan_when_below():
    from services.orchestrator.graph import ambiguity_router

    assert ambiguity_router(_make_state(ambiguity=0.2)) == "plan"
    assert ambiguity_router(_make_state(ambiguity=0.0)) == "plan"
    # Missing key defaults to plan.
    assert ambiguity_router(_make_state()) == "plan"


# ---------------------------------------------------------------------------
# 4. Compiled-graph integration: high-ambiguity halts at END w/o plan/execute
# ---------------------------------------------------------------------------

def _root_state(description: str) -> dict:
    return {
        "session_id": "s-clar",
        "root_goal": description,
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
        "error": None,
        "final_answer": "",
    }


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_high_ambiguity_halts_graph_before_plan_and_execute(monkeypatch):
    from services.orchestrator import graph as graph_mod
    from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator
    from langgraph.checkpoint.memory import MemorySaver

    async def fake_emit(type, **fields):
        pass

    monkeypatch.setattr(graph_mod.events, "emit", fake_emit)

    # If plan were reached, route() would be invoked; track it to assert it is NOT.
    fake_router = MagicMock()
    fake_router.route = AsyncMock(return_value=MagicMock())
    fake_router.runner.catalog_prompt.return_value = "CATALOG"

    mock_orch = MagicMock(spec=CodingOrchestrator)
    # High ambiguity -> the graph must halt at END before plan.
    mock_orch.architect = AsyncMock(return_value=(
        '{"assumptions": ["undefined referent"], "ambiguity": 0.85, '
        '"blocking_question": "Which thing should I improve?"}'
    ))
    mock_orch.skill_router = fake_router

    mock_async_orch = MagicMock(spec=AsyncOrchestrator)
    mock_async_orch.plan_and_dispatch = AsyncMock()

    real_cp = MemorySaver()
    with patch("pymongo.MongoClient", return_value=MagicMock()):
        with patch("langgraph.checkpoint.mongodb.MongoDBSaver", return_value=real_cp):
            graph, _ = graph_mod.build_graph(mock_orch, mock_async_orch)

    final_state = await graph.ainvoke(
        _root_state("make it better"),
        {"configurable": {"thread_id": "t-high-ambiguity"}},
    )

    # Halted on clarification with the blocking question; no guessed answer.
    assert final_state.get("awaiting_clarification") is True
    assert final_state.get("clarification_question") == "Which thing should I improve?"
    assert not final_state.get("final_answer")

    # Neither plan (route) nor execute (plan_and_dispatch) was reached.
    fake_router.route.assert_not_called()
    mock_async_orch.plan_and_dispatch.assert_not_called()
    assert final_state["goal_tree"]["root"]["children"] == []
