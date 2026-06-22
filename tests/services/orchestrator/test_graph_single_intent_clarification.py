from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_single_intent_clarification_pauses_not_decompose(monkeypatch):
    """FIX 2: a genuine single ambiguous intent (sub_intents == [goal_desc]) with
    needs_clarification=True must pause-and-clarify (set awaiting_clarification and
    emit clarification_request), NOT fall through to the architect decompose path."""
    from services.orchestrator import graph as graph_mod
    from services.orchestrator.skill_router import RouteResult
    from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

    captured: list[tuple[str, dict]] = []

    async def fake_emit(type, **fields):
        captured.append((type, fields))

    monkeypatch.setattr(graph_mod.events, "emit", fake_emit)

    goal_desc = "improve the thing"
    # Single intent, identical to goal_desc — this is the escape-hatch case.
    route_result = RouteResult(
        skills=[],
        needs_clarification=True,
        clarification_question="What do you mean by 'the thing'?",
        sub_intents=[goal_desc],
    )
    fake_router = MagicMock()
    fake_router.route = AsyncMock(return_value=route_result)
    fake_router.runner.catalog_prompt.return_value = "CATALOG"

    mock_orch = MagicMock(spec=CodingOrchestrator)
    # architect must NOT be used; if the escape hatch fires it would call this.
    mock_orch.architect = AsyncMock(return_value="Decomposed task 1\nDecomposed task 2")
    mock_orch.skill_router = fake_router
    mock_async_orch = MagicMock(spec=AsyncOrchestrator)

    plan_node, *_ = graph_mod.make_nodes(mock_orch, mock_async_orch)

    state = {
        "session_id": "s1",
        "goal_tree": {
            "root": {
                "id": "root", "parent_id": None, "children": [],
                "description": goal_desc,
                "status": "PENDING", "result": None, "error": None,
                "attempts": 0, "started_at": None, "updated_at": None,
            }
        },
        "current_goal_id": "root",
    }

    out = await plan_node(state)

    # Must pause for clarification, not decompose-and-proceed.
    assert out.get("awaiting_clarification") is True
    assert out.get("clarification_question") == "What do you mean by 'the thing'?"
    # No goal_tree decomposition should have happened.
    assert "goal_tree" not in out

    # clarification_request emitted with the right fields.
    types_emitted = [t for t, _ in captured]
    assert "clarification_request" in types_emitted
    clar = next(f for t, f in captured if t == "clarification_request")
    assert clar["question"] == "What do you mean by 'the thing'?"
    assert clar["task"] == goal_desc
    assert clar["session_id"] == "s1"

    # The architect decompose path must NOT have run.
    mock_orch.architect.assert_not_called()


@pytest.mark.asyncio
async def test_route_none_still_falls_back_to_architect(monkeypatch):
    """Backward-compat guard: when route() raises (caught) -> route_result is None,
    the plan node MUST still fall through to the architect decompose path."""
    from services.orchestrator import graph as graph_mod
    from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

    async def fake_emit(type, **fields):
        pass

    monkeypatch.setattr(graph_mod.events, "emit", fake_emit)

    fake_router = MagicMock()
    fake_router.route = AsyncMock(side_effect=TypeError("no live LLM"))
    fake_router.runner.catalog_prompt.return_value = "CATALOG"

    mock_orch = MagicMock(spec=CodingOrchestrator)
    mock_orch.architect = AsyncMock(return_value="Subtask A\nSubtask B")
    mock_orch.skill_router = fake_router
    mock_async_orch = MagicMock(spec=AsyncOrchestrator)

    plan_node, *_ = graph_mod.make_nodes(mock_orch, mock_async_orch)

    state = {
        "session_id": "s1",
        "goal_tree": {
            "root": {
                "id": "root", "parent_id": None, "children": [],
                "description": "do a clear task",
                "status": "PENDING", "result": None, "error": None,
                "attempts": 0, "started_at": None, "updated_at": None,
            }
        },
        "current_goal_id": "root",
    }

    out = await plan_node(state)

    # Fallback path ran: architect was called and children created.
    mock_orch.architect.assert_awaited_once()
    assert out.get("awaiting_clarification") in (False, None)
    tree = out["goal_tree"]
    assert len(tree["root"]["children"]) == 2
