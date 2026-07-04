from unittest.mock import MagicMock

import pytest

from services.orchestrator import events
from services.orchestrator.events import (
    current_task_id,
    read_and_clear_steer,
    write_steer,
)
from services.orchestrator.inproc_bus import SignalRegistry


@pytest.mark.asyncio
async def test_write_then_read_returns_text_and_clears():
    signals = SignalRegistry()
    await write_steer(signals, "t-1", "work on db.py instead")
    first = await read_and_clear_steer(signals, "t-1")
    assert first == "work on db.py instead"
    # Consume-once semantics: a second read sees nothing.
    assert await read_and_clear_steer(signals, "t-1") is None


@pytest.mark.asyncio
async def test_read_absent_is_none():
    signals = SignalRegistry()
    assert await read_and_clear_steer(signals, "missing") is None


@pytest.mark.asyncio
async def test_read_swallows_registry_error():
    signals = MagicMock()
    signals.read_and_clear_steer.side_effect = RuntimeError("registry broken")
    assert await read_and_clear_steer(signals, "t-err") is None


@pytest.mark.asyncio
async def test_current_task_id_from_active_emitter_else_none():
    assert current_task_id() is None
    em = events.EventEmitter(MagicMock(), "task-abc")
    token = events.current_emitter.set(em)
    try:
        assert current_task_id() == "task-abc"
    finally:
        events.current_emitter.reset(token)
