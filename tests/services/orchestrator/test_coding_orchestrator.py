# tests/services/orchestrator/test_coding_orchestrator.py
from __future__ import annotations
import asyncio
import pytest
import graphlib
from unittest.mock import AsyncMock, MagicMock, patch

from services.orchestrator.coding_orchestrator import TokenBudget, AsyncOrchestrator, Result, SubTask


def _make_mock_response(content: str = "done") -> MagicMock:
    r = MagicMock()
    r.choices = [MagicMock()]
    r.choices[0].message.content = content
    return r


@pytest.mark.mocked
class TestTokenBudget:
    def test_init_applies_80_percent_margin(self):
        budget = TokenBudget(total=100_000)
        assert budget.remaining == 80_000

    def test_init_custom_margin(self):
        budget = TokenBudget(total=100_000, margin=0.5)
        assert budget.remaining == 50_000

    @pytest.mark.asyncio
    async def test_reserve_success_decrements_remaining(self):
        budget = TokenBudget(total=100_000)
        result = await budget.reserve(10_000)
        assert result is True
        assert budget.remaining == 70_000

    @pytest.mark.asyncio
    async def test_reserve_fails_when_exhausted(self):
        budget = TokenBudget(total=1_000)
        # total * 0.8 = 800; 900 > 800 so reserve fails
        result = await budget.reserve(900)
        assert result is False

    @pytest.mark.asyncio
    async def test_refund_restores_balance(self):
        budget = TokenBudget(total=100_000)
        await budget.reserve(10_000)
        await budget.refund(10_000)
        assert budget.remaining == 80_000

    @pytest.mark.asyncio
    async def test_concurrent_reserves_are_serialized(self):
        budget = TokenBudget(total=10_000)
        # total * 0.8 = 8000; each reserve asks for 4000
        # Only two can succeed (4000 + 4000 = 8000)
        results = await asyncio.gather(
            budget.reserve(4_000),
            budget.reserve(4_000),
            budget.reserve(4_000),
        )
        assert results.count(True) == 2
        assert budget.remaining == 0


@pytest.mark.mocked
class TestAsyncOrchestrator:
    @pytest.mark.asyncio
    async def test_raises_cycle_error_before_any_spawn(self):
        # Build subtasks with a cycle A -> B -> A
        subtasks = [
            SubTask(id="A", prompt="task A", deps={"B"}),
            SubTask(id="B", prompt="task B", deps={"A"}),
        ]
        dep_graph = {t.id: t.deps for t in subtasks}
        ts = graphlib.TopologicalSorter(dep_graph)
        with pytest.raises(graphlib.CycleError):
            ts.prepare()  # CycleError raised here, before any spawn

    @pytest.mark.asyncio
    async def test_parallel_dispatch_happy_path(self):
        orch = AsyncOrchestrator()

        async def fake_call_qwen(t: SubTask) -> str:
            return f"result for {t.id}"

        with patch.object(orch, "_call_qwen_worker", side_effect=fake_call_qwen):
            goals = [
                {"id": "g1", "description": "task 1", "children": [], "status": "PENDING"},
                {"id": "g2", "description": "task 2", "children": [], "status": "PENDING"},
            ]
            results = await orch.plan_and_dispatch(goals)
            assert len(results) == 2
            ids = {r.id for r in results}
            assert ids == {"g1", "g2"}
            assert all(r.ok for r in results)

    @pytest.mark.asyncio
    async def test_cancelled_error_is_reraised(self):
        orch = AsyncOrchestrator(budget=10_000)
        t = SubTask(id="x", prompt="test", est_tokens=100)

        async def raise_cancel(_):
            raise asyncio.CancelledError()

        with patch.object(orch, "_call_qwen_worker", side_effect=raise_cancel):
            with pytest.raises((asyncio.CancelledError, ExceptionGroup)):
                await orch._run_worker(t)

    @pytest.mark.asyncio
    async def test_aggregate_calls_architect_with_2000_budget(self):
        orch = AsyncOrchestrator()
        results = [
            Result(id="r1", summary="proposal 1", ok=True),
            Result(id="r2", summary="proposal 2", ok=True),
        ]
        mock_response = _make_mock_response("synthesized result")

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, return_value=mock_response) as mock_complete:
            result = await orch.aggregate("some task", results)
            call_kwargs = mock_complete.call_args.kwargs
            assert call_kwargs["extra_body"]["thinking_budget_tokens"] == 2000
            assert result.id == "aggregated"
            assert "synthesized result" in result.summary

    @pytest.mark.asyncio
    async def test_condense_truncates_long_output(self):
        orch = AsyncOrchestrator()
        long_text = "x" * 5000
        result = orch._condense("gid", long_text)
        assert len(result.summary) <= 2000
        assert result.ok is True
        assert result.id == "gid"


@pytest.mark.mocked
class TestCodingOrchestrator:
    def _make_orch(self):
        from services.orchestrator.coding_orchestrator import CodingOrchestrator
        return CodingOrchestrator(
            graph=MagicMock(),
            workspace_path="/tmp/workspace",
            docker_container="lm-sandbox",
        )

    @pytest.mark.asyncio
    async def test_architect_passes_thinking_budget_in_extra_body(self):
        orch = self._make_orch()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "here is the plan"

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, return_value=mock_response) as mock_complete:
            result = await orch.architect("decompose this", thinking_budget=3000)
            call_kwargs = mock_complete.call_args.kwargs
            assert call_kwargs["extra_body"]["thinking_budget_tokens"] == 3000
            assert call_kwargs["model"] == "openai/gemma-4-31b"
            assert result == "here is the plan"

    @pytest.mark.asyncio
    async def test_architect_tool_dispatch_uses_zero_budget(self):
        orch = self._make_orch()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "tool result"

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, return_value=mock_response) as mock_complete:
            await orch.architect("route this tool call", thinking_budget=0)
            call_kwargs = mock_complete.call_args.kwargs
            assert call_kwargs["extra_body"]["thinking_budget_tokens"] == 0

    @pytest.mark.asyncio
    async def test_editor_calls_qwen(self):
        orch = self._make_orch()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "patched code"

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, return_value=mock_response) as mock_complete:
            result = await orch.editor("fix this bug")
            call_kwargs = mock_complete.call_args.kwargs
            assert call_kwargs["model"] == "openai/qwen2.5-coder-32b"
            assert result == "patched code"

    def test_is_stuck_returns_false_when_not_repeated(self):
        orch = self._make_orch()
        assert orch.is_stuck("action_a") is False
        assert orch.is_stuck("action_b") is False
        assert orch.is_stuck("action_a") is False

    def test_is_stuck_returns_true_after_n_identical_actions(self):
        orch = self._make_orch()
        orch.is_stuck("same_action")
        orch.is_stuck("same_action")
        result = orch.is_stuck("same_action")
        assert result is True

    def test_is_stuck_resets_on_different_action(self):
        orch = self._make_orch()
        orch.is_stuck("same_action")
        orch.is_stuck("same_action")
        orch.is_stuck("different_action")  # breaks the streak
        assert orch.is_stuck("same_action") is False

    def test_execute_in_sandbox_success(self):
        orch = self._make_orch()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="test output", stderr="", returncode=0
            )
            result = orch.execute_in_sandbox("echo hello")
            assert result["ok"] is True
            assert result["stdout"] == "test output"
            assert result["exit_code"] == 0
            cmd = mock_run.call_args[0][0]
            assert "docker" in cmd
            assert "exec" in cmd
            assert "lm-sandbox" in cmd

    def test_execute_in_sandbox_failure(self):
        orch = self._make_orch()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="", stderr="command not found", returncode=1
            )
            result = orch.execute_in_sandbox("bad_command")
            assert result["ok"] is False
            assert result["exit_code"] == 1

    def test_git_checkpoint_calls_git_add_and_commit(self):
        orch = self._make_orch()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            orch.git_checkpoint("step 3: fixed tests")
            calls = [c[0][0] for c in mock_run.call_args_list]
            assert any("add" in c for c in calls)
            assert any("commit" in c for c in calls)
