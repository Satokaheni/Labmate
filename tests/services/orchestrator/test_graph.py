# tests/services/orchestrator/test_graph.py
from __future__ import annotations
import pytest
from contextvars import copy_context
from unittest.mock import AsyncMock, MagicMock

from services.orchestrator.types import (
    Status, create_goal, update_status, get_ready_goals,
)
from services.orchestrator import events


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
        "root_goal": "top-level task",
        "last_artifact": {"type": "other", "payload": ""},
        "verified": False,
        "critique_score": 1.0,
        "critique_notes": "",
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

    @pytest.mark.asyncio
    async def test_plan_node_includes_skill_catalog_when_available(self):
        """Plan node should include skill catalog in the prompt when skill_router present."""
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator
        from unittest.mock import patch

        # Create a mock skill_router with a catalog
        mock_runner = MagicMock()
        mock_runner.catalog_prompt.return_value = "- test-skill: A test skill\n- other-skill: Another skill"
        mock_skill_router = MagicMock()
        mock_skill_router.runner = mock_runner

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.architect = AsyncMock(return_value="Use test-skill")
        mock_orch.skill_router = mock_skill_router
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        plan_node, *_ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        await plan_node(state)

        # Verify architect was called with a prompt containing skill info
        call_args = mock_orch.architect.call_args
        assert call_args is not None
        prompt = call_args[0][0]
        assert "test-skill" in prompt or "skill" in prompt.lower()

    @pytest.mark.asyncio
    async def test_plan_node_with_real_skill_router_property_access(self):
        """Test plan node with real SkillRouter.runner property (not private _runner)."""
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator
        from services.orchestrator.skill_router import SkillRouter
        from services.skill_runner.skill_runner import SkillRunner, SkillMeta
        from pathlib import Path

        # Build a real SkillRunner with a mock catalog
        runner = MagicMock(spec=SkillRunner)
        runner.catalog = {
            "test-skill": SkillMeta(
                name="test-skill",
                description="Test skill",
                path=Path("/fake/SKILL.md"),
                tier="bundled",
            )
        }
        runner.catalog_prompt.return_value = "- test-skill: Test skill"
        runner.tool_schema.return_value = {"type": "function", "function": {"name": "load_skill"}}

        # Create a real SkillRouter (which now has a .runner property)
        mock_redis = AsyncMock()
        skill_router = SkillRouter(
            runner=runner,
            redis=mock_redis,
            gemma_api_base="http://localhost:8000/v1",
        )

        # Verify the property is accessible (the PRIMARY BUG FIX)
        assert skill_router.runner is runner
        assert skill_router.runner.catalog_prompt() == "- test-skill: Test skill"

        # Now use it in the graph
        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.architect = AsyncMock(return_value="Use test-skill")
        mock_orch.skill_router = skill_router
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        plan_node, *_ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        delta = await plan_node(state)

        # Verify the plan executed without AttributeError
        assert "goal_tree" in delta
        # Verify architect was called with catalog prompt
        call_args = mock_orch.architect.call_args
        prompt = call_args[0][0]
        assert "Test skill" in prompt

    @pytest.mark.asyncio
    async def test_plan_node_emits_reasoning_event(self, fake_event_emitter):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.architect = AsyncMock(return_value="Subtask 1\nSubtask 2\nSubtask 3")
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        plan_node, *_ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        delta = await plan_node(state)

        # Verify event was emitted
        assert len(fake_event_emitter.events) == 1
        event_type, fields = fake_event_emitter.events[0]
        assert event_type == "reasoning"
        assert fields["node"] == "plan"
        assert "decomposed into 3 subtask(s)" in fields["summary"]
        assert "Subtask" in fields["text"]
        assert len(delta["goal_tree"]["root"]["children"]) == 3


@pytest.mark.mocked
class TestExecuteNode:
    @pytest.mark.asyncio
    async def test_idempotency_guard_skips_completed_goal(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)
        # No ready goals, so plan_and_dispatch should not be called
        mock_async_orch.plan_and_dispatch = AsyncMock()

        _, execute_node, *_ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        state["step_markers"]["root"] = "completed"
        # Mark root as completed so there are no ready goals
        update_status(state["goal_tree"], "root", Status.COMPLETED)

        delta = await execute_node(state)
        assert delta == {}
        mock_async_orch.plan_and_dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_node_calls_plan_and_dispatch_on_success(self):
        """execute_node should call plan_and_dispatch for ready goals."""
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator, Result

        mock_orch = MagicMock(spec=CodingOrchestrator)
        result = Result(id="root", summary="task completed", ok=True)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)
        mock_async_orch.plan_and_dispatch = AsyncMock(return_value=[result])

        _, execute_node, *_ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        await execute_node(state)
        mock_async_orch.plan_and_dispatch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_execute_node_handles_failure_from_plan_and_dispatch(self):
        """execute_node should mark goal FAILED when plan_and_dispatch returns ok=False."""
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator, Result

        mock_orch = MagicMock(spec=CodingOrchestrator)
        # Return a failed result
        result = Result(id="root", summary="error occurred", ok=False)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)
        mock_async_orch.plan_and_dispatch = AsyncMock(return_value=[result])

        _, execute_node, *_ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        delta = await execute_node(state)
        tree = delta["goal_tree"]
        assert tree["root"]["status"] == Status.FAILED.value

    @pytest.mark.asyncio
    async def test_execute_node_respects_idempotency_guard_on_retry(self):
        """execute_node should skip already-applied results on retry (idempotency) — FIX #4."""
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator, Result

        mock_orch = MagicMock(spec=CodingOrchestrator)
        # plan_and_dispatch returns a result
        result = Result(id="root", summary="already done", ok=True)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)
        mock_async_orch.plan_and_dispatch = AsyncMock(return_value=[result])

        _, execute_node, *_ = make_nodes(mock_orch, mock_async_orch)
        state = _make_state()
        # Mark goal with per-goal-ID "completed" marker (FIX #4: not hash-of-summary)
        state["step_markers"]["root"] = "completed"

        delta = await execute_node(state)
        # plan_and_dispatch is called, but idempotency guard prevents double-update
        mock_async_orch.plan_and_dispatch.assert_awaited_once()
        # The returned goal_tree should skip the update (because the marker matches)
        # The status stays PENDING since the idempotency guard skipped the update
        assert delta.get("goal_tree", {}).get("root", {}).get("status") == Status.PENDING.value

    @pytest.mark.asyncio
    async def test_execute_node_routes_all_ready_goals_through_plan_and_dispatch(self):
        """execute_node should use plan_and_dispatch for all ready goals (including single)."""
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator, Result

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        # Mock plan_and_dispatch to return a result
        result = Result(id="root", summary="task completed", ok=True)
        mock_async_orch.plan_and_dispatch = AsyncMock(return_value=[result])

        _, execute_node, *_ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        new_state = await execute_node(state)

        # Verify plan_and_dispatch was called
        mock_async_orch.plan_and_dispatch.assert_awaited_once()
        # Verify the goal was marked as completed
        assert new_state["goal_tree"]["root"]["status"] == Status.COMPLETED.value
        assert new_state["goal_tree"]["root"]["result"] == "task completed"

    @pytest.mark.asyncio
    async def test_execute_node_with_multiple_ready_goals_calls_plan_and_dispatch(self):
        """execute_node handles multiple ready goals via plan_and_dispatch."""
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator, Result

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        # Mock plan_and_dispatch to return results for both goals
        results = [
            Result(id="g1", summary="goal 1 done", ok=True),
            Result(id="g2", summary="goal 2 done", ok=True),
        ]
        mock_async_orch.plan_and_dispatch = AsyncMock(return_value=results)

        _, execute_node, *_ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        # Add two child goals
        create_goal(state["goal_tree"], "g1", "root", "Goal 1")
        create_goal(state["goal_tree"], "g2", "root", "Goal 2")
        # Mark root as completed so children are ready
        update_status(state["goal_tree"], "root", Status.COMPLETED)

        new_state = await execute_node(state)

        # Verify plan_and_dispatch was called with both goals
        assert mock_async_orch.plan_and_dispatch.await_count == 1
        called_goals = mock_async_orch.plan_and_dispatch.call_args[0][0]
        assert len(called_goals) == 2

        # Both goals should be marked completed
        assert new_state["goal_tree"]["g1"]["status"] == Status.COMPLETED.value
        assert new_state["goal_tree"]["g2"]["status"] == Status.COMPLETED.value

    @pytest.mark.asyncio
    async def test_execute_node_increments_attempts_on_failed_result(self):
        """execute_node should increment attempts counter when a goal fails (SECONDARY BUG FIX)."""
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator, Result

        mock_orch = MagicMock(spec=CodingOrchestrator)
        # Return a failed result
        result = Result(id="root", summary="error", ok=False)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)
        mock_async_orch.plan_and_dispatch = AsyncMock(return_value=[result])

        _, execute_node, *_ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        initial_attempts = state["goal_tree"]["root"].get("attempts", 0)
        delta = await execute_node(state)

        # Attempts should be incremented on failed result
        assert delta["goal_tree"]["root"]["attempts"] == initial_attempts + 1
        assert delta["goal_tree"]["root"]["status"] == Status.FAILED.value

    @pytest.mark.asyncio
    async def test_execute_node_idempotency_allows_retry_with_different_outcome(self):
        """execute_node idempotency (FIX #4): failed goals not marked, so retries can succeed."""
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator, Result

        mock_orch = MagicMock(spec=CodingOrchestrator)
        # First call: failure
        result1 = Result(id="root", summary="first error", ok=False)
        # Second call: success (after reflect retry)
        result2 = Result(id="root", summary="second success", ok=True)

        # On first call, return failure
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)
        mock_async_orch.plan_and_dispatch = AsyncMock(return_value=[result1])

        _, execute_node, *_ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        delta1 = await execute_node(state)

        # Verify first result is applied
        assert delta1["goal_tree"]["root"]["status"] == Status.FAILED.value
        assert "first error" in delta1["goal_tree"]["root"]["result"]
        marker_after_first = delta1["step_markers"].get("root")
        # Failed goals are NOT marked (FIX #4), so marker should not exist
        assert marker_after_first is None

        # Now simulate reflect resetting to PENDING
        state["goal_tree"]["root"]["status"] = Status.PENDING.value
        state["goal_tree"]["root"]["attempts"] = 1
        state["step_markers"] = delta1["step_markers"]

        # Second call: success
        mock_async_orch.plan_and_dispatch = AsyncMock(return_value=[result2])
        delta2 = await execute_node(state)

        # Verify second result (success) is applied because failed goal wasn't marked
        assert delta2["goal_tree"]["root"]["status"] == Status.COMPLETED.value
        assert "second success" in delta2["goal_tree"]["root"]["result"]
        marker_after_second = delta2["step_markers"].get("root")
        # Successful goal IS marked as "completed" (FIX #4)
        assert marker_after_second == "completed"


@pytest.mark.mocked
class TestExecuteNodeArtifact:
    @pytest.mark.asyncio
    async def test_execute_node_sets_last_artifact_code(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator, Result

        mock_orch = MagicMock(spec=CodingOrchestrator)
        result = Result(id="root", summary="def add(a, b):\n    return a + b", ok=True)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)
        mock_async_orch.plan_and_dispatch = AsyncMock(return_value=[result])

        _, execute_node, *_ = make_nodes(mock_orch, mock_async_orch)
        state = _make_state()
        delta = await execute_node(state)
        assert delta["last_artifact"]["type"] == "code"
        assert "def add" in delta["last_artifact"]["payload"]

    @pytest.mark.asyncio
    async def test_execute_node_sets_last_artifact_writing(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator, Result

        prose = ("This paper investigates the effect of context length on retrieval "
                 "accuracy across several configurations. " * 4)
        mock_orch = MagicMock(spec=CodingOrchestrator)
        result = Result(id="root", summary=prose, ok=True)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)
        mock_async_orch.plan_and_dispatch = AsyncMock(return_value=[result])

        _, execute_node, *_ = make_nodes(mock_orch, mock_async_orch)
        state = _make_state()
        delta = await execute_node(state)
        assert delta["last_artifact"]["type"] == "writing"


@pytest.mark.mocked
class TestCheckNodeEvents:
    @pytest.mark.asyncio
    async def test_check_node_emits_reasoning_event_on_finalize(self, fake_event_emitter):
        """Check node should emit reasoning event when finalizing."""
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        _, _, check_node, _, _, _, _ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        create_goal(state["goal_tree"], "task1", "root", "Task 1")
        create_goal(state["goal_tree"], "task2", "root", "Task 2")
        update_status(state["goal_tree"], "task1", Status.COMPLETED, result="Task 1 done")
        update_status(state["goal_tree"], "task2", Status.COMPLETED, result="Task 2 done")

        delta = await check_node(state)

        # Verify event was emitted
        assert len(fake_event_emitter.events) == 1
        event_type, fields = fake_event_emitter.events[0]
        assert event_type == "reasoning"
        assert fields["node"] == "check"
        assert "finalizing" in fields["summary"]

    @pytest.mark.asyncio
    async def test_check_node_emits_reasoning_event_on_defer(self, fake_event_emitter):
        """Check node should emit reasoning event when deferring to reflect."""
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        _, _, check_node, _, _, _, _ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        create_goal(state["goal_tree"], "task1", "root", "Task 1")
        create_goal(state["goal_tree"], "task2", "root", "Task 2")
        update_status(state["goal_tree"], "task1", Status.COMPLETED, result="Task 1 done")
        update_status(state["goal_tree"], "task2", Status.FAILED, error="Task 2 failed")
        state["goal_tree"]["task2"]["attempts"] = 1

        delta = await check_node(state)

        # Verify event was emitted
        assert len(fake_event_emitter.events) == 1
        event_type, fields = fake_event_emitter.events[0]
        assert event_type == "reasoning"
        assert fields["node"] == "check"
        assert "deferring" in fields["summary"]


@pytest.mark.mocked
class TestCheckNode:
    @pytest.mark.asyncio
    async def test_check_returns_empty_when_no_children(self):
        """Check node should return empty dict when root has no children."""
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        _, _, check_node, _, _, _, _ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        # Root has no children
        delta = await check_node(state)
        assert delta == {}

    @pytest.mark.asyncio
    async def test_check_defers_finalization_when_failed_child_retryable(self):
        """FIX #1: check should NOT finalize when a failed child has attempts < 3."""
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        _, _, check_node, _, _, _, _ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        # Add two children: one completed, one failed with attempts < 3
        create_goal(state["goal_tree"], "task1", "root", "Task 1")
        create_goal(state["goal_tree"], "task2", "root", "Task 2")
        update_status(state["goal_tree"], "task1", Status.COMPLETED, result="Task 1 done")
        update_status(state["goal_tree"], "task2", Status.FAILED, error="Task 2 failed")
        state["goal_tree"]["task2"]["attempts"] = 1

        delta = await check_node(state)

        # Check should NOT finalize; should return current_goal_id for the failed child and tree
        assert "final_answer" not in delta
        assert delta.get("current_goal_id") == "task2"
        assert "goal_tree" in delta
        # Root should still be in its current state, not finalized
        assert delta["goal_tree"]["root"]["status"] != Status.COMPLETED.value

    @pytest.mark.asyncio
    async def test_check_finalizes_with_failed_children_after_exhausted_attempts(self):
        """FIX #1: check should finalize with root=FAILED when all children terminal and no retryables."""
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        _, _, check_node, _, _, _, _ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        # One completed, one failed with exhausted attempts
        create_goal(state["goal_tree"], "task1", "root", "Task 1")
        create_goal(state["goal_tree"], "task2", "root", "Task 2")
        update_status(state["goal_tree"], "task1", Status.COMPLETED, result="Task 1 done")
        update_status(state["goal_tree"], "task2", Status.FAILED, error="Task 2 failed")
        state["goal_tree"]["task2"]["attempts"] = 3

        delta = await check_node(state)

        # Should finalize with root=FAILED
        assert "final_answer" in delta
        assert "error" in delta
        assert delta["goal_tree"]["root"]["status"] == Status.FAILED.value
        assert "Task 2 failed" in delta["final_answer"]

    @pytest.mark.asyncio
    async def test_check_finalizes_with_mixed_terminal_states_now_fails(self):
        """UPDATE: check should finalize with root=FAILED when any child FAILED and attempts >= 3."""
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        _, _, check_node, _, _, _, _ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        create_goal(state["goal_tree"], "task1", "root", "Task 1")
        create_goal(state["goal_tree"], "task2", "root", "Task 2")

        # One completed, one failed with max attempts
        update_status(state["goal_tree"], "task1", Status.COMPLETED, result="Task 1 done")
        update_status(state["goal_tree"], "task2", Status.FAILED, error="Task 2 failed")
        state["goal_tree"]["task2"]["attempts"] = 3

        delta = await check_node(state)

        # Should finalize since both are terminal and no retryables
        assert "goal_tree" in delta
        # Root should be FAILED, not COMPLETED (FIX #1)
        assert delta["goal_tree"]["root"]["status"] == Status.FAILED.value
        assert "error" in delta
        assert "Task 2 failed" in delta["final_answer"]

    @pytest.mark.asyncio
    async def test_check_returns_empty_when_children_not_all_terminal(self):
        """Check node should return empty dict when not all children are in terminal status."""
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        _, _, check_node, _, _, _, _ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        # Add children to root
        create_goal(state["goal_tree"], "task1", "root", "Task 1")
        create_goal(state["goal_tree"], "task2", "root", "Task 2")

        # Mark task1 as completed but leave task2 pending
        update_status(state["goal_tree"], "task1", Status.COMPLETED, result="Task 1 done")

        delta = await check_node(state)
        assert delta == {}

    @pytest.mark.asyncio
    async def test_check_finalizes_when_all_children_completed(self):
        """Check node should finalize root and set final_answer when all children are COMPLETED."""
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        _, _, check_node, _, _, _, _ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        # Add completed children
        create_goal(state["goal_tree"], "task1", "root", "Write documentation")
        create_goal(state["goal_tree"], "task2", "root", "Run tests")
        update_status(state["goal_tree"], "task1", Status.COMPLETED, result="Documentation written")
        update_status(state["goal_tree"], "task2", Status.COMPLETED, result="All tests passed")

        delta = await check_node(state)

        # Check that all expected keys are present
        assert "goal_tree" in delta
        assert "final_answer" in delta
        assert "current_goal_id" in delta

        # Root should now be COMPLETED
        assert delta["goal_tree"]["root"]["status"] == Status.COMPLETED.value
        assert delta["current_goal_id"] == "root"

        # final_answer should contain summaries from completed children
        assert "Documentation written" in delta["final_answer"]
        assert "All tests passed" in delta["final_answer"]

    @pytest.mark.asyncio
    async def test_check_defers_finalization_on_retryable_failure(self):
        """Check node should defer finalization when a failed child has attempts < 3 (even with mixed terminal)."""
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        _, _, check_node, _, _, _, _ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        create_goal(state["goal_tree"], "task1", "root", "Task 1")
        create_goal(state["goal_tree"], "task2", "root", "Task 2")

        # One completed, one failed (but retryable: attempts < 3)
        update_status(state["goal_tree"], "task1", Status.COMPLETED, result="Task 1 done")
        update_status(state["goal_tree"], "task2", Status.FAILED, error="Task 2 failed")
        state["goal_tree"]["task2"]["attempts"] = 1

        delta = await check_node(state)

        # Should defer finalization and route to reflect
        assert "final_answer" not in delta
        assert delta.get("current_goal_id") == "task2"

    @pytest.mark.asyncio
    async def test_check_uses_default_answer_when_no_results(self):
        """Check node should use 'Task completed.' when no child has a result."""
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        _, _, check_node, _, _, _, _ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        create_goal(state["goal_tree"], "task1", "root", "Task 1")
        # Mark as completed but don't set result
        update_status(state["goal_tree"], "task1", Status.COMPLETED)

        delta = await check_node(state)

        assert "final_answer" in delta
        assert delta["final_answer"] == "Task completed."


@pytest.mark.mocked
class TestReflectNode:
    @pytest.mark.asyncio
    async def test_reflect_resets_goal_to_pending_and_appends_message(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.architect = AsyncMock(return_value="do it differently next time")
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        _, _, _, reflect_node, _, _, _ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        update_status(state["goal_tree"], "root", Status.FAILED, error="syntax error")
        state["goal_tree"]["root"]["attempts"] = 1

        delta = await reflect_node(state)
        assert delta["goal_tree"]["root"]["status"] == Status.PENDING.value
        assert len(delta["messages"]) == 1
        assert delta["messages"][0]["role"] == "reflection"
        assert "differently" in delta["messages"][0]["content"]

    @pytest.mark.asyncio
    async def test_reflect_emits_reasoning_event(self, fake_event_emitter):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.architect = AsyncMock(return_value="Try a different approach: check for edge cases")
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        _, _, _, reflect_node, _, _, _ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        update_status(state["goal_tree"], "root", Status.FAILED, error="assertion failed")
        state["goal_tree"]["root"]["attempts"] = 1

        delta = await reflect_node(state)

        # Verify event was emitted
        assert len(fake_event_emitter.events) == 1
        event_type, fields = fake_event_emitter.events[0]
        assert event_type == "reasoning"
        assert fields["node"] == "reflect"
        assert "diagnosing failed subtask" in fields["summary"]
        assert "edge cases" in fields["text"]


@pytest.fixture
def fake_event_emitter():
    """Fixture that provides a fake EventEmitter for testing event emissions."""
    class FakeEventEmitter:
        def __init__(self):
            self.events = []

        async def emit(self, type: str, **fields):
            self.events.append((type, fields))

    emitter = FakeEventEmitter()
    # Set it in the ContextVar for this test
    token = events.current_emitter.set(emitter)
    yield emitter
    # Reset after test
    events.current_emitter.reset(token)


@pytest.mark.mocked
class TestAssessAmbiguityNode:
    @pytest.mark.asyncio
    async def test_assess_ambiguity_parses_json(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.architect = AsyncMock(return_value=(
            '{"assumptions": ["uses python"], "ambiguity": 0.8, '
            '"blocking_question": "which framework?"}'
        ))
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        nodes = make_nodes(mock_orch, mock_async_orch)
        assess = nodes[5]  # 6th node

        state = _make_state(root_goal="build a thing")
        delta = await assess(state)
        assert delta["ambiguity"] == 0.8
        assert delta["assumptions"] == ["uses python"]
        assert delta["blocking_question"] == "which framework?"

    @pytest.mark.asyncio
    async def test_assess_ambiguity_defaults_zero_on_bad_json(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.architect = AsyncMock(return_value="not json at all")
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        nodes = make_nodes(mock_orch, mock_async_orch)
        assess = nodes[5]

        state = _make_state(root_goal="anything")
        delta = await assess(state)
        assert delta["ambiguity"] == 0.0
        assert delta["assumptions"] == []

    @pytest.mark.asyncio
    async def test_assess_ambiguity_strips_code_fences(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.architect = AsyncMock(return_value=(
            '```json\n{"assumptions": [], "ambiguity": 0.3, "blocking_question": ""}\n```'
        ))
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        nodes = make_nodes(mock_orch, mock_async_orch)
        assess = nodes[5]

        state = _make_state(root_goal="x")
        delta = await assess(state)
        assert delta["ambiguity"] == 0.3

    @pytest.mark.asyncio
    async def test_assess_ambiguity_emits_reasoning_event(self, fake_event_emitter):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        # Low ambiguity (< AMBIGUITY_THRESHOLD) so ONLY the assess_ambiguity event fires.
        mock_orch.architect = AsyncMock(return_value=(
            '{"assumptions": ["uses python"], "ambiguity": 0.3, "blocking_question": "which version?"}'
        ))
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        nodes = make_nodes(mock_orch, mock_async_orch)
        assess = nodes[5]

        state = _make_state(root_goal="build a python app")
        delta = await assess(state)

        # Verify event was emitted
        assert len(fake_event_emitter.events) == 1
        event_type, fields = fake_event_emitter.events[0]
        assert event_type == "reasoning"
        assert fields["node"] == "assess_ambiguity"
        assert "ambiguity=0.30" in fields["summary"]
        assert "1 assumption(s)" in fields["summary"]
        assert delta["ambiguity"] == 0.3


@pytest.mark.mocked
class TestApprovalNode:
    @pytest.mark.asyncio
    async def test_approval_node_does_not_emit(self, fake_event_emitter):
        """Approval re-runs on every resume; it must NOT emit (would duplicate the event).
        (The approval node stays reachable via the check->approval edge; FIX 7 only changed
        the assess_ambiguity path to halt for clarification instead of routing here.)"""
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        _, _, _, _, approval_node, _, _ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        update_status(state["goal_tree"], "root", Status.AWAITING_APPROVAL)

        # approval calls interrupt(), which raises. No event should be emitted either way.
        try:
            await approval_node(state)
        except Exception:
            pass

        assert fake_event_emitter.events == []

    @pytest.mark.asyncio
    async def test_assess_ambiguity_emits_clarification_event_when_ambiguous(self, fake_event_emitter):
        """FIX 7: high-ambiguity tasks now HALT for clarification (ask a blocking question)
        instead of routing to the approval interrupt. Previously this asserted a
        node='approval' 'awaiting human approval' reasoning event; now assess_ambiguity
        emits a clarification_request event (same shape the plan node uses)."""
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.architect = AsyncMock(return_value=(
            '{"assumptions": ["x"], "ambiguity": 0.9, "blocking_question": "clarify?"}'
        ))
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        nodes = make_nodes(mock_orch, mock_async_orch)
        assess = nodes[5]

        delta = await assess(_make_state(root_goal="vague task"))

        # The assess_ambiguity score event still fires.
        nodes_emitted = [fields.get("node") for _, fields in fake_event_emitter.events]
        assert "assess_ambiguity" in nodes_emitted
        # No approval reasoning event any more.
        assert "approval" not in nodes_emitted
        # A clarification_request event is emitted with the blocking question.
        clar = [(name, f) for name, f in fake_event_emitter.events if name == "clarification_request"]
        assert len(clar) == 1
        assert clar[0][1]["question"] == "clarify?"
        # The node halts for clarification.
        assert delta["awaiting_clarification"] is True
        assert delta["clarification_question"] == "clarify?"


@pytest.mark.mocked
class TestVerifyNode:
    @pytest.mark.asyncio
    async def test_verify_passes_through_non_code_writing(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.skill_router = None
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        nodes = make_nodes(mock_orch, mock_async_orch)
        verify = nodes[6]

        state = _make_state(last_artifact={"type": "other", "payload": "ok"})
        delta = await verify(state)
        assert delta["verified"] is True
        assert delta["critique_score"] == 1.0

    @pytest.mark.asyncio
    async def test_verify_calls_critique_for_code(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_router = MagicMock()
        mock_router.execute = AsyncMock(return_value={
            "ok": True,
            "result": {"score": 0.42, "notes": "missing error handling"},
        })
        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.skill_router = mock_router
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        nodes = make_nodes(mock_orch, mock_async_orch)
        verify = nodes[6]

        state = _make_state(last_artifact={"type": "code", "payload": "def f(): pass"})
        delta = await verify(state)
        mock_router.execute.assert_awaited_once()
        assert delta["critique_score"] == 0.42
        assert delta["verified"] is True

    @pytest.mark.asyncio
    async def test_verify_defaults_score_when_critique_fails(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_router = MagicMock()
        mock_router.execute = AsyncMock(return_value={"ok": False, "error": "timeout"})
        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.skill_router = mock_router
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        nodes = make_nodes(mock_orch, mock_async_orch)
        verify = nodes[6]

        state = _make_state(last_artifact={"type": "code", "payload": "x=1"})
        delta = await verify(state)
        assert delta["critique_score"] == 1.0
        assert delta["verified"] is True

    @pytest.mark.asyncio
    async def test_verify_emits_reasoning_event_for_code_artifact(self, fake_event_emitter):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_router = MagicMock()
        mock_router.execute = AsyncMock(return_value={
            "ok": True,
            "result": {"score": 0.85, "notes": "good code but missing error handling"},
        })
        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.skill_router = mock_router
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        nodes = make_nodes(mock_orch, mock_async_orch)
        verify = nodes[6]

        state = _make_state(last_artifact={"type": "code", "payload": "def f(): pass"})
        delta = await verify(state)

        # Verify event was emitted
        assert len(fake_event_emitter.events) == 1
        event_type, fields = fake_event_emitter.events[0]
        assert event_type == "reasoning"
        assert fields["node"] == "verify"
        assert "critique_score=0.85" in fields["summary"]
        assert "error handling" in fields["text"]
        assert delta["critique_score"] == 0.85

    @pytest.mark.asyncio
    async def test_verify_emits_reasoning_event_when_skipped(self, fake_event_emitter):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.skill_router = None
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        nodes = make_nodes(mock_orch, mock_async_orch)
        verify = nodes[6]

        state = _make_state(last_artifact={"type": "other", "payload": "some data"})
        delta = await verify(state)

        # Verify event was emitted even when skipped
        assert len(fake_event_emitter.events) == 1
        event_type, fields = fake_event_emitter.events[0]
        assert event_type == "reasoning"
        assert fields["node"] == "verify"
        assert "skipped" in fields["summary"]
        assert delta["verified"] is True
        assert delta["critique_score"] == 1.0


@pytest.mark.mocked
class TestVerifyRouter:
    def test_verify_router_routes_to_reflect_when_below_threshold(self):
        from services.orchestrator.graph import verify_router
        state = _make_state(critique_score=0.5)
        assert verify_router(state) == "reflect"

    def test_verify_router_routes_to_check_when_at_threshold(self):
        from services.orchestrator.graph import verify_router
        state = _make_state(critique_score=0.95)
        assert verify_router(state) == "check"

    def test_verify_router_defaults_to_check_when_missing(self):
        from services.orchestrator.graph import verify_router
        state = _make_state()
        assert verify_router(state) == "check"


@pytest.mark.mocked
class TestAmbiguityRouter:
    def test_ambiguity_router_halts_for_clarification_when_high(self):
        # FIX 7: high ambiguity now halts (END) to ask the user a clarifying question,
        # rather than routing to the approval interrupt.
        from services.orchestrator.graph import ambiguity_router
        from langgraph.graph import END
        state = _make_state(ambiguity=0.7)
        assert ambiguity_router(state) == END

    def test_ambiguity_router_routes_to_plan_when_low(self):
        from services.orchestrator.graph import ambiguity_router
        state = _make_state(ambiguity=0.2)
        assert ambiguity_router(state) == "plan"

    def test_ambiguity_router_defaults_to_plan_when_missing(self):
        from services.orchestrator.graph import ambiguity_router
        state = _make_state()
        assert ambiguity_router(state) == "plan"


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

    def test_build_graph_wires_assess_ambiguity_node(self):
        from unittest.mock import patch
        from services.orchestrator.graph import build_graph
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator
        from langgraph.checkpoint.memory import MemorySaver

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)
        real_cp = MemorySaver()
        with patch("pymongo.MongoClient"):
            with patch("langgraph.checkpoint.mongodb.MongoDBSaver", return_value=real_cp):
                graph, _ = build_graph(mock_orch, mock_async_orch)
                assert "assess_ambiguity" in set(graph.nodes.keys())

    def test_build_graph_wires_verify_node(self):
        from unittest.mock import patch
        from services.orchestrator.graph import build_graph
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator
        from langgraph.checkpoint.memory import MemorySaver

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)
        real_cp = MemorySaver()
        with patch("pymongo.MongoClient"):
            with patch("langgraph.checkpoint.mongodb.MongoDBSaver", return_value=real_cp):
                graph, _ = build_graph(mock_orch, mock_async_orch)
                assert "verify" in set(graph.nodes.keys())


@pytest.mark.mocked
class TestEndToEndGraphExecution:
    """End-to-end tests for subtask failure and recovery paths (FIX #5)."""

    @pytest.mark.asyncio
    async def test_e2e_subtask_failure_retry_success(self):
        """
        FIX #5: Drive the full graph through a failure-retry-success path.
        - Architect returns 2 subtasks
        - First execute: one COMPLETED, one FAILED (attempts=1)
        - Check: defers finalization, routes to reflect
        - Reflect: resets to PENDING with reflection message
        - Execute again: both COMPLETED
        - Check: finalizes with root COMPLETED
        """
        from services.orchestrator.graph import build_graph
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator, Result
        from langgraph.checkpoint.memory import MemorySaver
        from unittest.mock import patch

        mock_orch = MagicMock(spec=CodingOrchestrator)
        # Plan returns 2 subtasks
        mock_orch.architect = AsyncMock(return_value="Subtask 1\nSubtask 2")
        mock_orch.skill_router = None

        mock_async_orch = MagicMock(spec=AsyncOrchestrator)
        # First execute: one success, one failure
        first_results = [
            Result(id="root_sub0", summary="Subtask 1 complete", ok=True),
            Result(id="root_sub1", summary="Subtask 2 error", ok=False),
        ]
        # Second execute (after reflect): both success
        second_results = [
            Result(id="root_sub1", summary="Subtask 2 complete (retry)", ok=True),
        ]
        mock_async_orch.plan_and_dispatch = AsyncMock(
            side_effect=[first_results, second_results]
        )

        real_cp = MemorySaver()
        with patch("pymongo.MongoClient"):
            with patch("langgraph.checkpoint.mongodb.MongoDBSaver", return_value=real_cp):
                graph, _ = build_graph(mock_orch, mock_async_orch)

                # Run the graph
                initial_state = {
                    "session_id": "test-e2e-1",
                    "goal_tree": {},
                    "current_goal_id": "root",
                    "step_markers": {},
                    "messages": [],
                    "error": None,
                    "final_answer": "",
                    "workspace_id": "ws1",
                    "user_id": "user1",
                }
                create_goal(initial_state["goal_tree"], "root", None, "Do two tasks")

                final_state = await graph.ainvoke(initial_state, {"configurable": {"thread_id": "test-e2e-1"}})

                # Verify final state
                assert final_state["goal_tree"]["root"]["status"] == Status.COMPLETED.value
                assert "final_answer" in final_state
                assert final_state.get("error") is None
                # Verify both children are completed
                root = final_state["goal_tree"]["root"]
                children = root["children"]
                for cid in children:
                    assert final_state["goal_tree"][cid]["status"] == Status.COMPLETED.value
                # Verify reflection happened (message appended)
                reflection_messages = [m for m in final_state.get("messages", []) if m.get("role") == "reflection"]
                assert len(reflection_messages) > 0

    @pytest.mark.asyncio
    async def test_e2e_subtask_exhausted_attempts_finalizes_failed(self):
        """
        FIX #5: Subtask fails until exhausted, then root finalizes with FAILED status.
        - Architect returns 1 subtask
        - Execute: FAILED (attempts=1)
        - Check/Reflect/Execute cycle repeats until attempts == MAX_GOAL_ATTEMPTS
        - Check: no retryables remain; finalizes with root FAILED and error set
        FIX 9: the retry cap is now MAX_GOAL_ATTEMPTS (default 2), not the old hardcoded 3,
        so the final attempts count is asserted against the constant. The failure summary
        ("error: always fails") is retryable (no environmental marker), so attempts increments
        by 1 per pass up to MAX_GOAL_ATTEMPTS.
        """
        from services.orchestrator.graph import build_graph
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator, Result
        from langgraph.checkpoint.memory import MemorySaver
        from unittest.mock import patch

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.architect = AsyncMock(return_value="Subtask that always fails")
        mock_orch.skill_router = None

        mock_async_orch = MagicMock(spec=AsyncOrchestrator)
        # Always return FAILED
        failed_result = Result(id="root_sub0", summary="error: always fails", ok=False)
        mock_async_orch.plan_and_dispatch = AsyncMock(return_value=[failed_result])

        real_cp = MemorySaver()
        with patch("pymongo.MongoClient"):
            with patch("langgraph.checkpoint.mongodb.MongoDBSaver", return_value=real_cp):
                graph, _ = build_graph(mock_orch, mock_async_orch)

                initial_state = {
                    "session_id": "test-e2e-2",
                    "goal_tree": {},
                    "current_goal_id": "root",
                    "step_markers": {},
                    "messages": [],
                    "error": None,
                    "final_answer": "",
                    "workspace_id": "ws1",
                    "user_id": "user1",
                }
                create_goal(initial_state["goal_tree"], "root", None, "Impossible task")

                final_state = await graph.ainvoke(initial_state, {"configurable": {"thread_id": "test-e2e-2"}})

                # Verify final state: root FAILED, error set
                assert final_state["goal_tree"]["root"]["status"] == Status.FAILED.value
                assert "error" in final_state
                assert final_state.get("final_answer") != ""
                # FIX 9: child should be exhausted at MAX_GOAL_ATTEMPTS (was hardcoded 3).
                from services.orchestrator.graph import MAX_GOAL_ATTEMPTS
                children = final_state["goal_tree"]["root"]["children"]
                if children:
                    child_id = children[0]
                    assert final_state["goal_tree"][child_id]["attempts"] == MAX_GOAL_ATTEMPTS
