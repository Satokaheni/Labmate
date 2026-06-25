"""Integration tests for the orchestrator background compaction sweeper."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_background_compactor_compacts_idle_session(monkeypatch):
    """A session idle past the threshold with high fill triggers a background compact
    and its reflections are written to memory."""
    import services.orchestrator.main as main

    # Wake the sweeper almost immediately and inspect a small batch.
    monkeypatch.setattr(main, "BG_COMPACT_INTERVAL_S", 0)
    monkeypatch.setattr(main, "BG_COMPACT_MAX_SESSIONS", 20)

    proc = main.OrchestratorProcess()

    # orch only needs a _gemma_base attribute for the bg llm closure.
    orch = MagicMock()
    orch._gemma_base = "http://localhost:8000/v1"

    # context_manager.maybe_background_compact returns a compact result with reflections.
    context_manager = MagicMock()
    context_manager.maybe_background_compact = AsyncMock(return_value={
        "summary_tokens": 40,
        "pruned_messages": 6,
        "reflections": ["use Redis streams"],
    })

    consolidator = MagicMock()
    consolidator.write_reflections = AsyncMock()

    storage = MagicMock()
    storage.context_manager = context_manager
    storage.consolidator = consolidator
    # storage._db["messages"].distinct(...) → one candidate session.
    messages_col = MagicMock()
    messages_col.distinct = AsyncMock(return_value=["sess-1"])
    storage._db = {"messages": messages_col}

    # Run one sweep, then signal shutdown so the loop exits.
    task = asyncio.create_task(proc._background_compactor(orch, storage))
    # Give the sweeper time to perform at least one iteration.
    for _ in range(50):
        await asyncio.sleep(0)
        if context_manager.maybe_background_compact.await_count:
            break
    proc._shutdown.set()
    await asyncio.wait_for(task, timeout=2.0)

    context_manager.maybe_background_compact.assert_awaited()
    assert context_manager.maybe_background_compact.await_args[0][0] == "sess-1"
    # Reflections written fire-and-forget; let the created task run.
    await asyncio.sleep(0)
    consolidator.write_reflections.assert_awaited_with("sess-1", ["use Redis streams"])


@pytest.mark.asyncio
async def test_background_compactor_skips_when_maybe_returns_none(monkeypatch):
    """When maybe_background_compact returns None (not idle / low fill), no reflections
    are written and the sweep continues without error."""
    import services.orchestrator.main as main

    monkeypatch.setattr(main, "BG_COMPACT_INTERVAL_S", 0)
    monkeypatch.setattr(main, "BG_COMPACT_MAX_SESSIONS", 20)

    proc = main.OrchestratorProcess()
    orch = MagicMock()
    orch._gemma_base = "http://localhost:8000/v1"

    context_manager = MagicMock()
    context_manager.maybe_background_compact = AsyncMock(return_value=None)

    consolidator = MagicMock()
    consolidator.write_reflections = AsyncMock()

    storage = MagicMock()
    storage.context_manager = context_manager
    storage.consolidator = consolidator
    messages_col = MagicMock()
    messages_col.distinct = AsyncMock(return_value=["sess-1"])
    storage._db = {"messages": messages_col}

    task = asyncio.create_task(proc._background_compactor(orch, storage))
    for _ in range(50):
        await asyncio.sleep(0)
        if context_manager.maybe_background_compact.await_count:
            break
    proc._shutdown.set()
    await asyncio.wait_for(task, timeout=2.0)

    context_manager.maybe_background_compact.assert_awaited()
    consolidator.write_reflections.assert_not_awaited()
