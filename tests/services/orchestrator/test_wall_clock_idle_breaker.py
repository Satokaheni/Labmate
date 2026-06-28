from __future__ import annotations

import json
import time

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.orchestrator.coding_orchestrator import AsyncOrchestrator
from tests.conftest import run_async


def _bash_msg(command: str):
    tc = MagicMock()
    tc.id = f"call-{command}"
    tc.function = MagicMock()
    tc.function.name = "run_bash"
    tc.function.arguments = json.dumps({"command": command})
    msg = MagicMock()
    msg.tool_calls = [tc]
    msg.content = ""
    msg.reasoning_content = ""
    msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    return MagicMock(choices=[MagicMock(message=msg)])


def _orch_with_clock(clock):
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp", now=clock)
    mcp = AsyncMock()
    res = MagicMock()
    res.content = [MagicMock(text="output")]
    res.isError = False
    mcp.call_tool.return_value = res
    orch.mcp = mcp
    return orch


@pytest.mark.mocked
def test_now_defaults_to_time_monotonic():
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    assert orch._now is time.monotonic


@pytest.mark.mocked
def test_now_is_injectable():
    calls = {"n": 0}

    def fake_clock() -> float:
        calls["n"] += 1
        return float(calls["n"])

    orch = AsyncOrchestrator(
        skill_router=None, mcp=None, workspace="/tmp", now=fake_clock
    )
    assert orch._now is fake_clock
    assert orch._now() == 1.0
    assert orch._now() == 2.0


@pytest.mark.mocked
def test_wall_clock_deadline_stops_loop(monkeypatch):
    monkeypatch.setenv("LABMATE_GOAL_DEADLINE_S", "5")
    monkeypatch.setenv("LABMATE_NOPROGRESS_LIMIT", "0")  # isolate the deadline

    ticks = iter([0.0, 4.0, 8.0, 12.0, 16.0])  # start, then per-turn reads
    orch = _orch_with_clock(lambda: next(ticks))
    orch.max_steps = 10

    responses = [_bash_msg("echo 1"), _bash_msg("echo 2"), _bash_msg("echo 3")]
    with patch(
        "services.orchestrator.coding_orchestrator.litellm.acompletion",
        new_callable=AsyncMock, side_effect=responses,
    ) as mock:
        result = run_async(orch.react_execute("spin past the deadline"))

    assert result["ok"] is False
    assert "wall-clock deadline exceeded" in result["summary"]
    # start@0; turn1 reads 4 (<=5, runs); turn2 reads 8 (>5) -> stop. 1 call? No:
    # start consumes one tick, turn1 reads tick #2 (=4), runs model call #1,
    # turn2 reads tick #3 (=8) -> stop before call #2 => exactly 1 model call.
    assert mock.await_count == 1


@pytest.mark.mocked
def test_deadline_zero_disables(monkeypatch):
    monkeypatch.setenv("LABMATE_GOAL_DEADLINE_S", "0")
    monkeypatch.setenv("LABMATE_NOPROGRESS_LIMIT", "0")
    # A clock that would blow any positive deadline, but deadline is disabled.
    ticks = iter([0.0] + [10_000.0] * 10)
    orch = _orch_with_clock(lambda: next(ticks))
    orch.max_steps = 2

    # finish on turn 1 so the loop ends cleanly via normal completion.
    fin = MagicMock()
    fin.id = "call-finish"
    fin.function = MagicMock()
    fin.function.name = "finish"
    fin.function.arguments = json.dumps({"summary": "done, deadline off"})
    fmsg = MagicMock()
    fmsg.tool_calls = [fin]
    fmsg.content = ""
    fmsg.reasoning_content = ""
    fmsg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    finish_resp = MagicMock(choices=[MagicMock(message=fmsg)])

    with patch(
        "services.orchestrator.coding_orchestrator.litellm.acompletion",
        new_callable=AsyncMock, side_effect=[finish_resp],
    ):
        result = run_async(orch.react_execute("disabled deadline"))

    assert result["ok"] is True
    assert "done, deadline off" in result["summary"]


_noprogress_counter = {"count": 0}


def _noprogress_msg():
    """A turn that calls a tool but yields no new assistant content — the
    degenerate 'spinning' turn the breaker is meant to catch.

    It is NOT finish and produces empty content; we use run_bash so the loop
    keeps going (a no-tool-call turn would return early). The step def treats
    these as no-progress via the made_progress rule (empty content + this turn
    flagged as not advancing). For the unit test we drive the breaker directly
    by scripting turns that the loop counts as idle.
    """
    _noprogress_counter["count"] += 1
    # Use a unique command each time so the loop detector doesn't trigger
    return _bash_msg(f"noop_{_noprogress_counter['count']}")


@pytest.mark.mocked
def test_no_progress_breaker_trips_at_limit(monkeypatch):
    monkeypatch.setenv("LABMATE_GOAL_DEADLINE_S", "0")   # isolate the breaker
    monkeypatch.setenv("LABMATE_NOPROGRESS_LIMIT", "3")
    _noprogress_counter["count"] = 0  # reset counter for this test

    orch = _orch_with_clock(lambda: 0.0)
    orch.max_steps = 20

    # Force every turn to be counted as no-progress by stubbing the progress
    # decision to False (see Step 3 for the seam). Here we script enough turns.
    responses = [_noprogress_msg() for _ in range(10)]
    with patch(
        "services.orchestrator.coding_orchestrator.litellm.acompletion",
        new_callable=AsyncMock, side_effect=responses,
    ) as mock, patch.object(
        AsyncOrchestrator, "_turn_made_progress", return_value=False
    ):
        result = run_async(orch.react_execute("make no progress forever"))

    assert result["ok"] is False
    assert "no-progress breaker tripped" in result["summary"]
    assert "3" in result["summary"]
    assert mock.await_count == 3


@pytest.mark.mocked
def test_no_progress_limit_zero_disables(monkeypatch):
    monkeypatch.setenv("LABMATE_GOAL_DEADLINE_S", "0")
    monkeypatch.setenv("LABMATE_NOPROGRESS_LIMIT", "0")
    _noprogress_counter["count"] = 0  # reset counter for this test

    orch = _orch_with_clock(lambda: 0.0)
    orch.max_steps = 2  # IterationBudget still bounds the loop

    responses = [_noprogress_msg() for _ in range(10)]
    with patch(
        "services.orchestrator.coding_orchestrator.litellm.acompletion",
        new_callable=AsyncMock, side_effect=responses,
    ), patch.object(AsyncOrchestrator, "_turn_made_progress", return_value=False):
        result = run_async(orch.react_execute("breaker disabled"))

    # Breaker off -> IterationBudget ends it ("budget exhausted"), not the breaker.
    assert result["ok"] is False
    assert "no-progress breaker tripped" not in result["summary"]
