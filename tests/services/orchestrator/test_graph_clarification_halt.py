"""Tests for the clarification-halt fix (FIX 1).

When the plan node sets state["awaiting_clarification"] = True, the graph must
NOT proceed to execute_node — it must halt at END so the agent asks a clarifying
question WITHOUT also guessing an answer to the ambiguous task.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Unit tests for clarification_router
# ---------------------------------------------------------------------------


@pytest.mark.mocked
class TestClarificationRouter:
    def test_returns_end_when_awaiting_clarification(self):
        from langgraph.graph import END

        from services.orchestrator.graph import clarification_router

        state = {"awaiting_clarification": True}
        assert clarification_router(state) == END

    def test_returns_execute_when_not_awaiting_clarification(self):
        from services.orchestrator.graph import clarification_router

        assert clarification_router({"awaiting_clarification": False}) == "execute"

    def test_returns_execute_when_flag_missing(self):
        from services.orchestrator.graph import clarification_router

        assert clarification_router({}) == "execute"

    def test_returns_execute_when_flag_falsey(self):
        from services.orchestrator.graph import clarification_router

        assert clarification_router({"awaiting_clarification": None}) == "execute"


# ---------------------------------------------------------------------------
# Integration: the compiled graph must halt at plan on clarification, never
# reaching execute_node (no skill execution / no guessed final_answer).
# ---------------------------------------------------------------------------


@pytest.mark.mocked
class TestGraphHaltsOnClarification:
    @pytest.mark.asyncio
    async def test_graph_does_not_reach_execute_node_on_clarification(self, monkeypatch):
        # Clarification is now owned exclusively by the assess_ambiguity gate (route()
        # never clarifies after the multi-intent removal). This drives a HIGH-ambiguity
        # task so assess_ambiguity sets awaiting_clarification; ambiguity_router then
        # halts at END BEFORE plan/execute. The graph must ask the question without
        # guessing an answer or running any skill.
        from langgraph.checkpoint.memory import MemorySaver

        from services.orchestrator import graph as graph_mod
        from services.orchestrator.coding_orchestrator import (
            AsyncOrchestrator,
            CodingOrchestrator,
        )

        # Silence event emission.
        async def fake_emit(type, **fields):
            pass

        monkeypatch.setattr(graph_mod.events, "emit", fake_emit)

        fake_router = MagicMock()
        fake_router.route = AsyncMock()  # should never be reached on high ambiguity
        fake_router.runner.catalog_prompt.return_value = "CATALOG"

        mock_orch = MagicMock(spec=CodingOrchestrator)
        # assess_ambiguity calls architect; return a HIGH-ambiguity JSON with a
        # blocking question so ambiguity_router halts at END.
        mock_orch.architect = AsyncMock(
            return_value='{"assumptions": [], "ambiguity": 0.95, '
            '"blocking_question": "Did you want to search or generate?"}'
        )
        mock_orch.skill_router = fake_router

        # If execute_node is ever reached, plan_and_dispatch would be invoked.
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)
        mock_async_orch.plan_and_dispatch = AsyncMock(return_value=[])

        real_cp = MemorySaver()

        with patch("services.orchestrator.graph._make_sqlite_checkpointer", return_value=real_cp):
            graph, _ = graph_mod.build_graph(mock_orch, mock_async_orch)

        initial_state = {
            "session_id": "s1",
            "root_goal": "make it better",
            "goal_tree": {
                "root": {
                    "id": "root",
                    "parent_id": None,
                    "children": [],
                    "description": "make it better",
                    "status": "PENDING",
                    "result": None,
                    "error": None,
                    "attempts": 0,
                    "started_at": None,
                    "updated_at": None,
                }
            },
            "current_goal_id": "root",
            "step_markers": {},
        }

        config = {"configurable": {"thread_id": "t-clarify"}}
        final_state = await graph.ainvoke(initial_state, config)

        # The graph must have halted on clarification:
        assert final_state.get("awaiting_clarification") is True
        assert final_state.get("clarification_question") == "Did you want to search or generate?"

        # It must NOT have reached plan (route) or execute_node.
        fake_router.route.assert_not_called()
        mock_async_orch.plan_and_dispatch.assert_not_called()

        # And it must NOT have guessed a final answer to the ambiguous task.
        assert not final_state.get("final_answer")

        # No child goals were created.
        assert final_state["goal_tree"]["root"]["children"] == []

    @pytest.mark.asyncio
    async def test_graph_reaches_execute_when_no_clarification(self, monkeypatch):
        """Regression guard: when route() returns skills (no clarification), the
        graph proceeds through plan -> execute (plan_and_dispatch IS called)."""
        from langgraph.checkpoint.memory import MemorySaver

        from services.orchestrator import graph as graph_mod
        from services.orchestrator.coding_orchestrator import (
            AsyncOrchestrator,
            CodingOrchestrator,
        )
        from services.orchestrator.skill_router import RouteResult

        async def fake_emit(type, **fields):
            pass

        monkeypatch.setattr(graph_mod.events, "emit", fake_emit)

        route_result = RouteResult(
            skills=["dataset-search"],
            needs_clarification=False,
            sub_intents=["search a dataset"],
        )
        fake_router = MagicMock()
        fake_router.route = AsyncMock(return_value=route_result)
        fake_router.runner.catalog_prompt.return_value = "CATALOG"

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.architect = AsyncMock(
            return_value='{"assumptions": [], "ambiguity": 0.0, "blocking_question": ""}'
        )
        mock_orch.skill_router = fake_router

        # Result object mirroring what plan_and_dispatch yields.
        result_obj = MagicMock()
        result_obj.id = None  # set below to the created child id
        result_obj.ok = True
        result_obj.summary = "found a dataset"

        async def fake_dispatch(ready):
            # Mark the first ready goal as completed.
            g = ready[0]
            result_obj.id = g["id"]
            return [result_obj]

        mock_async_orch = MagicMock(spec=AsyncOrchestrator)
        mock_async_orch.plan_and_dispatch = AsyncMock(side_effect=fake_dispatch)

        real_cp = MemorySaver()

        with patch("services.orchestrator.graph._make_sqlite_checkpointer", return_value=real_cp):
            graph, _ = graph_mod.build_graph(mock_orch, mock_async_orch)

        initial_state = {
            "session_id": "s1",
            "root_goal": "search a dataset",
            "goal_tree": {
                "root": {
                    "id": "root",
                    "parent_id": None,
                    "children": [],
                    "description": "search a dataset",
                    "status": "PENDING",
                    "result": None,
                    "error": None,
                    "attempts": 0,
                    "started_at": None,
                    "updated_at": None,
                }
            },
            "current_goal_id": "root",
            "step_markers": {},
        }

        config = {"configurable": {"thread_id": "t-noclarify"}}
        final_state = await graph.ainvoke(initial_state, config)

        # No clarification was requested.
        assert not final_state.get("awaiting_clarification")
        # Execute node WAS reached.
        mock_async_orch.plan_and_dispatch.assert_called()
