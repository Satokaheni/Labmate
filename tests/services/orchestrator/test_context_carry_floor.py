"""Regression: the context strip must NOT reset toward 0 on every new message.

Root cause of the "every message resets" bug: at turn start the orchestrator emits
`context` with the carried fill, but the carried fill (`_session_ctx_fill`) was only
ever written from the per-task peak prompt_tokens counter — and that counter stays 0
whenever no completion reports usage (the streaming final-answer path, usage-less
endpoints). So the carry never seeded, every turn's `_carried_fill` was 0, and the
strip reset to 0% on message 2, 3, ...

Fix: the orchestrator measures the REAL current context size with build_context()
at turn start (Gemma-tokenized, reflects accumulated history) and uses it as a hard
FLOOR for the strip AND seeds the per-session carry from it immediately — so the
gauge shows the real fill at every turn start and the carry is non-zero even when the
turn reports no usage.

These tests drive OrchestratorProcess._handle end-to-end with mocked storage/orch
and assert (a) the emitted turn-start context is the measured floor (never 0)
and (b) the per-session carry is seeded from that floor with a zero peak counter.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.orchestrator import call_counter, events, main
from services.orchestrator.main import OrchestratorProcess


def _make_storage(total_tokens: int):
    """A StorageManager stub: only the surfaces _handle touches on the happy path."""
    storage = MagicMock()

    ctx = MagicMock()
    ctx.total_tokens = total_tokens
    storage.context_manager.build_context = AsyncMock(return_value=ctx)
    storage.context_manager.full_compact = AsyncMock(return_value={"pruned_messages": 0})

    storage.workspaces.load_agent_instructions = AsyncMock(return_value="")
    storage.workspaces.record_session = AsyncMock()
    storage.workspaces.upsert_workspace = AsyncMock()
    storage.workspaces.complete_session = AsyncMock()
    return storage


def _make_orch():
    """A CodingOrchestrator stub whose run_task succeeds and reports NO usage."""
    orch = MagicMock()
    orch._gemma_base = "http://localhost:8000/v1"
    orch.run_task = AsyncMock(return_value={"final_answer": "done", "error": None})
    # No streaming: return the assembled answer unchanged (still zero usage recorded).
    orch.stream_final_answer = AsyncMock(return_value="done")
    orch.architect = AsyncMock(return_value="")
    return orch


async def _run_handle(proc, storage, orch, session_id, task="hi"):
    """Invoke _handle once, capturing every emitted context-window payload."""
    payload = {"task_id": "t1", "task": task, "session_id": session_id}

    captured: list[dict] = []

    class _CapEmitter:
        def __init__(self, *a, **k):
            pass

        async def emit(self, name, **kw):
            if name == "context":
                captured.append(kw["window"])

    async def _mod_emit(name, **kw):
        if name == "context":
            captured.append(kw["window"])

    # Disable the live refresher so we only see the turn-start + turn-end emits.
    # The real current_emitter set/reset run fine with the _CapEmitter instance;
    # events.emit is patched to capture the module-level (turn-end) emit too.
    with (
        patch.object(main, "CONTEXT_REFRESH_S", 0.0),
        patch.object(events, "EventEmitter", _CapEmitter),
        patch.object(events, "emit", _mod_emit),
    ):
        await proc._handle(payload, orch, storage)

    return captured


@pytest.mark.asyncio
async def test_turn_start_context_is_measured_floor_not_zero():
    proc = OrchestratorProcess()

    storage = _make_storage(total_tokens=8000)
    orch = _make_orch()

    # Ensure no live peak is recorded (the exact condition that broke the carry).
    assert call_counter.get_peak_prompt_tokens() == 0

    windows = await _run_handle(proc, storage, orch, session_id="s-floor")

    assert windows, "expected at least one context emit"
    # The FIRST (turn-start) emit must show the real measured floor, never 0.
    assert windows[0]["used"] == 8000
    assert windows[0]["segments"]["conversation"] == 8000
    # And every emit this turn stays at/above the floor.
    assert all(w["used"] >= 8000 for w in windows)


@pytest.mark.asyncio
async def test_carry_seeded_from_floor_when_no_peak():
    proc = OrchestratorProcess()

    storage = _make_storage(total_tokens=8000)
    orch = _make_orch()

    await _run_handle(proc, storage, orch, session_id="s-carry")

    # Even though no completion reported usage (peak == 0), the session carry is
    # seeded from the measured floor — so the NEXT turn starts non-zero.
    assert proc._session_ctx_fill.get("s-carry") == 8000


@pytest.mark.asyncio
async def test_second_turn_carry_holds_across_messages():
    """The 'every message' case: two turns on the same session keep a non-zero floor."""
    proc = OrchestratorProcess()

    orch = _make_orch()

    # Turn 1: history is small.
    w1 = await _run_handle(proc, _make_storage(total_tokens=6000), orch, "s-multi")
    assert w1[0]["used"] == 6000
    assert proc._session_ctx_fill["s-multi"] == 6000

    # Turn 2: history grew; the turn-start emit shows the NEW larger floor, never 0.
    w2 = await _run_handle(proc, _make_storage(total_tokens=9000), orch, "s-multi")
    assert w2[0]["used"] == 9000
    assert w2[0]["used"] > 0  # the bug was this resetting to 0
    assert proc._session_ctx_fill["s-multi"] == 9000
