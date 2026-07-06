"""Durable suspend/resume coverage against a REAL LocalStore (SQLite).

Closes the coverage gap flagged in the lite-orchestrator spike review: the
existing lite_persistence tests use an in-memory `_FakeStore`, and the T6
approval tests pass NO store (so `save_suspend` is skipped entirely). Neither
exercises the actual `LocalStore.checkpoint_put/get/delete` round-trip — which
is the ONE place the lite engine differs mechanically from the graph engine
(hand-rolled snapshot resume vs. the LangGraph checkpointer). These tests drive
the real SQLite store end-to-end.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.orchestrator.inproc_bus import SignalRegistry
from services.orchestrator.lite_orchestrator import run_goal_lite
from services.orchestrator.lite_persistence import clear, load_resume, save_suspend
from services.orchestrator.lite_state import build_initial_state, snapshot
from services.orchestrator.local_store import LocalStore


def _real_store(tmp_path) -> LocalStore:
    return LocalStore(tmp_path / "state.db")


def _assess_orch() -> MagicMock:
    """orch whose ambiguity-assess returns low-ambiguity JSON (no halt)."""
    orch = MagicMock()
    orch.context_manager = None
    orch.architect = AsyncMock(return_value='{"ambiguity":0.0,"blocking_question":""}')
    return orch


# ---------------------------------------------------------------------------
# 1. Persistence primitive round-trip against real SQLite
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_load_clear_roundtrip_real_localstore(tmp_path):
    store = _real_store(tmp_path)
    state = build_initial_state("deploy the service", "sess-rt")

    await save_suspend(store, "sess-rt", state, "await_approval")

    # A brand-new LocalStore over the SAME file sees the persisted checkpoint —
    # proves it is DURABLE (survives the in-memory object), not just cached.
    reopened = LocalStore(tmp_path / "state.db")
    loaded = await load_resume(reopened, "sess-rt")
    assert loaded is not None
    got_state, phase = loaded
    assert phase == "await_approval"
    assert snapshot(got_state) == snapshot(state)  # state survives the round-trip

    await clear(reopened, "sess-rt")
    assert await load_resume(reopened, "sess-rt") is None  # cleared


@pytest.mark.asyncio
async def test_load_resume_absent_key_returns_none(tmp_path):
    store = _real_store(tmp_path)
    assert await load_resume(store, "never-saved") is None


# ---------------------------------------------------------------------------
# 2. run_goal_lite approval gate — durable suspend, external approve, resume
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_irreversible_goal_persists_checkpoint_then_resumes_on_approve(tmp_path):
    """The real durable path: an irreversible goal writes an `await_approval`
    checkpoint to the SQLite store, and an approve decision (as the ws_gateway
    would inject) resumes it to execution with the checkpoint cleared.

    A checkpoint_put spy records the suspend write while delegating to the real
    store, so the durable suspend is asserted without a concurrent task (a
    concurrent create_task races aiosqlite's worker-thread teardown at loop close
    — verified separately that await_approval resumes correctly mid-wait)."""
    store = _real_store(tmp_path)
    puts: list[tuple[str, dict]] = []
    real_put = store.checkpoint_put

    async def spy_put(task_id, payload):
        puts.append((task_id, payload))
        await real_put(task_id, payload)

    store.checkpoint_put = spy_put

    sig = SignalRegistry()
    sig.set_approval("sess-app", "approve")  # pre-approved -> await returns at once
    orch = _assess_orch()
    async_orch = MagicMock()
    async_orch.react_execute = AsyncMock(
        return_value={"ok": True, "summary": "deployed", "tests_passed": False}
    )

    out = await run_goal_lite(
        orch,
        async_orch,
        "deploy to production",
        "sess-app",
        store=store,
        signals=sig,
    )

    # Durable suspend actually happened: an await_approval checkpoint was written
    # to the REAL store (not skipped, not mocked).
    assert any(
        tid == "sess-app" and p.get("phase") == "await_approval" for tid, p in puts
    ), "irreversible goal did not persist an await_approval checkpoint"
    async_orch.react_execute.assert_awaited_once()  # resumed and executed
    assert out["ok"] is True
    assert await load_resume(store, "sess-app") is None  # cleared on resume


@pytest.mark.asyncio
async def test_irreversible_goal_reject_blocks_without_executing_and_clears(tmp_path):
    store = _real_store(tmp_path)
    sig = SignalRegistry()
    sig.set_approval("sess-rej", "reject")  # pre-set: await returns immediately
    orch = _assess_orch()
    async_orch = MagicMock()
    async_orch.react_execute = AsyncMock()

    out = await run_goal_lite(
        orch,
        async_orch,
        "delete the production database",
        "sess-rej",
        store=store,
        signals=sig,
    )

    async_orch.react_execute.assert_not_called()  # rejected -> never executed
    assert out["ok"] is False
    assert await load_resume(store, "sess-rej") is None  # checkpoint cleared
