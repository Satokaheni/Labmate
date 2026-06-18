# tests/services/orchestrator/test_graph.py
from __future__ import annotations
import pytest
from unittest.mock import AsyncMock, MagicMock

from services.orchestrator.types import (
    Status, create_goal, update_status, get_ready_goals,
)


def _make_state(**overrides) -> dict:
    tree: dict = {}
    create_goal(tree, "root", None, "top-level task")
    base = {
        "session_id": "test-001",
        "goal_tree": tree,
        "current_goal_id": "root",
        "step_markers": {},
        "messages": [],
        "error": None,
    }
    base.update(overrides)
    return base


@pytest.mark.mocked
class TestRouter:
    def test_router_returns_end_when_no_ready_goals(self):
        from services.orchestrator.graph import router
        from langgraph.graph import END

        state = _make_state()
        update_status(state["goal_tree"], "root", Status.COMPLETED)
        result = router(state)
        assert result == END

    def test_router_returns_end_when_current_goal_id_is_none(self):
        from services.orchestrator.graph import router
        from langgraph.graph import END

        state = _make_state(current_goal_id=None)
        result = router(state)
        assert result == END

    def test_router_returns_reflect_on_failed_goal_with_attempts_lt_3(self):
        from services.orchestrator.graph import router

        state = _make_state()
        update_status(state["goal_tree"], "root", Status.FAILED)
        state["goal_tree"]["root"]["attempts"] = 1
        result = router(state)
        assert result == "reflect"

    def test_router_returns_end_on_failed_goal_at_max_attempts(self):
        from services.orchestrator.graph import router
        from langgraph.graph import END

        state = _make_state()
        update_status(state["goal_tree"], "root", Status.FAILED)
        state["goal_tree"]["root"]["attempts"] = 3
        result = router(state)
        assert result == END

    def test_router_returns_approval_on_awaiting_approval(self):
        from services.orchestrator.graph import router

        state = _make_state()
        update_status(state["goal_tree"], "root", Status.AWAITING_APPROVAL)
        result = router(state)
        assert result == "approval"

    def test_router_returns_execute_when_ready_goals_exist(self):
        from services.orchestrator.graph import router

        state = _make_state()
        update_status(state["goal_tree"], "root", Status.COMPLETED)
        create_goal(state["goal_tree"], "child1", "root", "pending work")
        result = router(state)
        assert result == "execute"


@pytest.mark.mocked
class TestPlanNode:
    @pytest.mark.asyncio
    async def test_plan_node_creates_child_goals_from_architect_response(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.architect = AsyncMock(return_value="Subtask A\nSubtask B\nSubtask C")
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        plan_node, *_ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        delta = await plan_node(state)

        tree = delta["goal_tree"]
        children = tree["root"]["children"]
        assert len(children) == 3
        descriptions = {tree[c]["description"] for c in children}
        assert "Subtask A" in descriptions
        assert "Subtask B" in descriptions
        assert "Subtask C" in descriptions

    @pytest.mark.asyncio
    async def test_plan_node_skips_empty_lines(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.architect = AsyncMock(return_value="Task 1\n\nTask 2\n")
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        plan_node, *_ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        delta = await plan_node(state)
        children = delta["goal_tree"]["root"]["children"]
        assert len(children) == 2


@pytest.mark.mocked
class TestExecuteNode:
    @pytest.mark.asyncio
    async def test_idempotency_guard_skips_completed_goal(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        _, execute_node, *_ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        state["step_markers"]["root"] = "completed"

        delta = await execute_node(state)
        assert delta == {}
        mock_orch.execute_in_sandbox.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_node_calls_git_checkpoint_on_success(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.execute_in_sandbox.return_value = {
            "stdout": "Tests passed", "stderr": "", "exit_code": 0, "ok": True
        }
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        _, execute_node, *_ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        await execute_node(state)
        mock_orch.git_checkpoint.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_node_increments_attempts_on_failure(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.execute_in_sandbox.return_value = {
            "stdout": "", "stderr": "error", "exit_code": 1, "ok": False
        }
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        _, execute_node, *_ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        delta = await execute_node(state)
        tree = delta["goal_tree"]
        assert tree["root"]["attempts"] == 1
        assert tree["root"]["status"] == Status.FAILED.value

    @pytest.mark.asyncio
    async def test_execute_node_git_checkpoint_not_called_on_failure(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.execute_in_sandbox.return_value = {
            "stdout": "", "stderr": "fail", "exit_code": 2, "ok": False
        }
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        _, execute_node, *_ = make_nodes(mock_orch, mock_async_orch)
        state = _make_state()
        await execute_node(state)
        mock_orch.git_checkpoint.assert_not_called()


@pytest.mark.mocked
class TestReflectNode:
    @pytest.mark.asyncio
    async def test_reflect_resets_goal_to_pending_and_appends_message(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.architect = AsyncMock(return_value="do it differently next time")
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        _, _, _, reflect_node, _ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        update_status(state["goal_tree"], "root", Status.FAILED, error="syntax error")
        state["goal_tree"]["root"]["attempts"] = 1

        delta = await reflect_node(state)
        assert delta["goal_tree"]["root"]["status"] == Status.PENDING.value
        assert len(delta["messages"]) == 1
        assert delta["messages"][0]["role"] == "reflection"
        assert "differently" in delta["messages"][0]["content"]


@pytest.mark.mocked
class TestBuildGraph:
    def test_build_graph_compiles_without_error(self):
        from unittest.mock import patch, MagicMock as MM
        from services.orchestrator.graph import build_graph
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator
        from langgraph.checkpoint.memory import MemorySaver

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        # Mock MongoClient, use MemorySaver for MongoDBSaver
        mock_client = MagicMock()
        real_cp = MemorySaver()

        with patch("pymongo.MongoClient", return_value=mock_client):
            with patch("langgraph.checkpoint.mongodb.MongoDBSaver", return_value=real_cp):
                graph, cp = build_graph(mock_orch, mock_async_orch)
                assert graph is not None
                assert cp is real_cp

    def test_build_graph_wires_correct_nodes(self):
        from unittest.mock import patch
        from services.orchestrator.graph import build_graph
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator
        from langgraph.checkpoint.memory import MemorySaver

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        # Mock MongoClient, use MemorySaver for MongoDBSaver
        mock_client = MagicMock()
        real_cp = MemorySaver()

        with patch("pymongo.MongoClient", return_value=mock_client):
            with patch("langgraph.checkpoint.mongodb.MongoDBSaver", return_value=real_cp):
                graph, _ = build_graph(mock_orch, mock_async_orch)
                node_names = set(graph.nodes.keys())
                assert "plan" in node_names
                assert "execute" in node_names
                assert "check" in node_names
                assert "reflect" in node_names
                assert "approval" in node_names
