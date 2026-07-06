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
    async_orch.react_execute = AsyncMock(
        return_value={
            "ok": True,
            "summary": "def reverse(s): return s[::-1]",
            "tools_used": [],
            "tests_passed": False,
        }
    )
    out = await run_goal_lite(orch, async_orch, "reverse a string in python", "sess")
    # gate did not set a blocking-question final answer
    assert out.get("final_answer", "") == "" or "?" not in out.get("final_answer", "")


@pytest.mark.asyncio
async def test_non_ambiguous_task_executes_via_react():
    orch = MagicMock()
    orch.architect = AsyncMock(
        return_value='{"assumptions": [], "ambiguity": 0.1, "blocking_question": ""}'
    )
    orch.context_manager = None
    async_orch = MagicMock()
    async_orch.react_execute = AsyncMock(
        return_value={"ok": True, "summary": "2 + 2 is 4.", "tools_used": [], "tests_passed": False}
    )
    out = await run_goal_lite(orch, async_orch, "What is 2+2?", "sess")
    async_orch.react_execute.assert_awaited_once()
    assert out["final_answer"] == "2 + 2 is 4."
    assert out.get("ok") is True


@pytest.mark.asyncio
async def test_execute_carries_tests_passed_signal():
    orch = MagicMock()
    orch.architect = AsyncMock(return_value='{"ambiguity": 0.0, "blocking_question": ""}')
    orch.context_manager = None
    async_orch = MagicMock()
    async_orch.react_execute = AsyncMock(
        return_value={
            "ok": True,
            "summary": "fixed",
            "tools_used": ["write_file"],
            "tests_passed": True,
        }
    )
    out = await run_goal_lite(orch, async_orch, "fix the bug in x.py", "sess")
    assert out.get("tests_passed") is True
    assert out.get("ok") is True


@pytest.mark.asyncio
async def test_failing_goal_retries_then_finalizes():
    orch = MagicMock()
    orch.context_manager = None
    orch.architect = AsyncMock(
        side_effect=['{"ambiguity":0.0,"blocking_question":""}', "diagnosis: the fix was wrong"]
    )
    async_orch = MagicMock()
    async_orch.react_execute = AsyncMock(
        side_effect=[
            {"ok": False, "summary": "attempt 1 failed", "tests_passed": False},
            {"ok": True, "summary": "attempt 2 fixed it", "tests_passed": True},
        ]
    )
    out = await run_goal_lite(orch, async_orch, "fix the bug", "sess")
    assert async_orch.react_execute.await_count == 2  # retried once
    assert out["ok"] is True and out["tests_passed"] is True


@pytest.mark.asyncio
async def test_irreversible_reject_blocks_without_executing():
    from services.orchestrator.inproc_bus import SignalRegistry

    sig = SignalRegistry()
    sig.set_approval("sess", "reject")
    orch = MagicMock()
    orch.context_manager = None
    orch.architect = AsyncMock(return_value='{"ambiguity":0.0,"blocking_question":""}')
    async_orch = MagicMock()
    async_orch.react_execute = AsyncMock()
    out = await run_goal_lite(orch, async_orch, "deploy to production", "sess", signals=sig)
    async_orch.react_execute.assert_not_called()
    assert out["ok"] is False and "not approved" in out["final_answer"].lower()


@pytest.mark.asyncio
async def test_irreversible_approve_executes():
    from services.orchestrator.inproc_bus import SignalRegistry

    sig = SignalRegistry()
    sig.set_approval("sess", "approve")
    orch = MagicMock()
    orch.context_manager = None
    orch.architect = AsyncMock(return_value='{"ambiguity":0.0,"blocking_question":""}')
    async_orch = MagicMock()
    async_orch.react_execute = AsyncMock(
        return_value={"ok": True, "summary": "deployed", "tests_passed": False}
    )
    out = await run_goal_lite(orch, async_orch, "deploy to production", "sess", signals=sig)
    async_orch.react_execute.assert_awaited_once()
    assert out["ok"] is True
