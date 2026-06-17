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
