from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.orchestrator.lite_orchestrator import run_goal_lite


@pytest.mark.asyncio
async def test_ambiguous_task_halts_with_question():
    # orch.architect returns the assess JSON with high ambiguity + a blocking question.
    orch = MagicMock()
    orch.architect = AsyncMock(
        return_value='{"assumptions": [], "ambiguity": 0.9, "blocking_question": "Which file did you mean?"}'
    )
    orch.context_manager = None
    async_orch = MagicMock()
    async_orch.react_execute = AsyncMock()
    out = await run_goal_lite(orch, async_orch, "improve it", "sess")
    assert "Which file" in out["final_answer"]
    async_orch.react_execute.assert_not_called()  # halted before execution


@pytest.mark.asyncio
async def test_clear_task_does_not_halt_on_ambiguity():
    # Low ambiguity -> the gate does NOT halt (execution is added in Task 5; here just
    # assert final_answer is NOT the blocking question and react wasn't blocked by the gate).
    orch = MagicMock()
    orch.architect = AsyncMock(
        return_value='{"assumptions": ["reasonable default"], "ambiguity": 0.1, "blocking_question": ""}'
    )
    orch.context_manager = None
    async_orch = MagicMock()
    out = await run_goal_lite(orch, async_orch, "reverse a string in python", "sess")
    # gate did not set a blocking-question final answer
    assert out.get("final_answer", "") == "" or "?" not in out.get("final_answer", "")
