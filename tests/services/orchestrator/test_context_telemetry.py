"""Context-window telemetry: the strip must never sit at 0/0.

The orchestrator now emits a `context` event at turn start (max populated, 0 used),
refreshes it live while the turn runs, and emits a final accurate value at turn end.
These cover the pure window builder and the live refresher loop.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.orchestrator import call_counter, main


def test_context_window_zero_used_without_counter():
    # No per-task counter in this context → peak is 0, but max is always populated.
    w = main._context_window()
    assert w["max"] == main.CTX_TOKENS
    assert w["used"] == 0
    assert w["free"] == main.CTX_TOKENS
    assert w["segments"]["conversation"] == 0


def test_context_window_reports_peak_prompt_tokens():
    token = call_counter.start()
    try:
        call_counter.current_counter.get().observe_prompt_tokens(1234)
        w = main._context_window()
        assert w["used"] == 1234
        assert w["free"] == main.CTX_TOKENS - 1234
        assert w["segments"]["conversation"] == 1234
    finally:
        call_counter.reset(token)


def test_context_window_clamps_used_to_max():
    token = call_counter.start()
    try:
        call_counter.current_counter.get().observe_prompt_tokens(main.CTX_TOKENS + 5000)
        w = main._context_window()
        assert w["used"] == main.CTX_TOKENS
        assert w["free"] == 0
    finally:
        call_counter.reset(token)


def test_context_window_uses_fallback_when_no_peak():
    # Before this turn's first model call (peak 0), show the carried prior-turn fill
    # instead of 0 — so the strip doesn't reset to 0% on a new message.
    w = main._context_window(used_fallback=5000)
    assert w["used"] == 5000
    assert w["segments"]["conversation"] == 5000


def test_context_window_live_peak_overrides_fallback():
    # Once a real peak is recorded it wins over the carried fallback (so the value
    # still reflects compaction, which shrinks the prompt below the carried fill).
    token = call_counter.start()
    try:
        call_counter.current_counter.get().observe_prompt_tokens(1234)
        w = main._context_window(used_fallback=5000)
        assert w["used"] == 1234
    finally:
        call_counter.reset(token)


def test_context_window_fallback_clamped_to_max():
    w = main._context_window(used_fallback=main.CTX_TOKENS + 5000)
    assert w["used"] == main.CTX_TOKENS
    assert w["free"] == 0


def test_context_window_floor_applies_without_peak_or_fallback():
    # The known real context size (measured with build_context) is a hard floor:
    # even with no per-task peak and no carried fill, the strip shows the real fill
    # instead of resetting to 0% — this is the guarantee for every new message.
    w = main._context_window(used_floor=7000)
    assert w["used"] == 7000
    assert w["segments"]["conversation"] == 7000


def test_context_window_floor_wins_over_smaller_fallback():
    # Carried fill from the prior turn is smaller than the measured floor → floor wins,
    # so the gauge never dips below the real current context size.
    w = main._context_window(used_fallback=1000, used_floor=7000)
    assert w["used"] == 7000


def test_context_window_peak_wins_over_floor_when_larger():
    # A real measured peak above the floor is the true high-water mark and wins.
    token = call_counter.start()
    try:
        call_counter.current_counter.get().observe_prompt_tokens(20000)
        w = main._context_window(used_floor=7000)
        assert w["used"] == 20000
    finally:
        call_counter.reset(token)


def test_context_window_floor_wins_over_smaller_peak():
    # A tiny peak (e.g. a cheap tool-selection call with thinking_budget=0) must not
    # drag the gauge below the real measured context floor.
    token = call_counter.start()
    try:
        call_counter.current_counter.get().observe_prompt_tokens(500)
        w = main._context_window(used_floor=7000)
        assert w["used"] == 7000
    finally:
        call_counter.reset(token)


def test_context_window_floor_clamped_to_max():
    w = main._context_window(used_floor=main.CTX_TOKENS + 5000)
    assert w["used"] == main.CTX_TOKENS
    assert w["free"] == 0


@pytest.mark.asyncio
async def test_context_refresh_loop_emits_until_cancelled():
    emitter = MagicMock()
    emitter.emit = AsyncMock()

    task = asyncio.create_task(main._context_refresh_loop(emitter, interval_s=0.01))
    await asyncio.sleep(0.035)  # ~3 ticks
    task.cancel()
    await task  # loop swallows CancelledError and returns cleanly

    assert emitter.emit.await_count >= 1
    for call in emitter.emit.await_args_list:
        assert call.args[0] == "context"
        assert "window" in call.kwargs
        assert call.kwargs["window"]["max"] == main.CTX_TOKENS


@pytest.mark.asyncio
async def test_context_refresh_loop_survives_a_failed_emit():
    emitter = MagicMock()
    # First emit raises, loop must keep going (best-effort telemetry).
    emitter.emit = AsyncMock(side_effect=[RuntimeError("redis down"), None, None])

    task = asyncio.create_task(main._context_refresh_loop(emitter, interval_s=0.01))
    await asyncio.sleep(0.035)
    task.cancel()
    await task

    assert emitter.emit.await_count >= 2  # did not die on the first failure
