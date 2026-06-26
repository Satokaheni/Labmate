import pytest
import fakeredis.aioredis
from unittest.mock import AsyncMock, MagicMock

from services.orchestrator import events
from services.orchestrator.events import (
    STEER_PREFIX,
    write_steer,
    read_and_clear_steer,
    current_task_id,
)


@pytest.mark.asyncio
async def test_write_then_read_returns_text_and_clears():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await write_steer(r, "t-1", "work on db.py instead")
    first = await read_and_clear_steer(r, "t-1")
    assert first == "work on db.py instead"
    # GETDEL semantics: a second read sees nothing.
    assert await read_and_clear_steer(r, "t-1") is None
    assert await r.exists(f"{STEER_PREFIX}t-1") == 0


@pytest.mark.asyncio
async def test_read_absent_is_none():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    assert await read_and_clear_steer(r, "missing") is None


@pytest.mark.asyncio
async def test_read_swallows_redis_error():
    r = MagicMock()
    r.getdel = AsyncMock(side_effect=RuntimeError("redis down"))
    assert await read_and_clear_steer(r, "t-err") is None


@pytest.mark.asyncio
async def test_current_task_id_from_active_emitter_else_none():
    assert current_task_id() is None
    em = events.EventEmitter(MagicMock(), "task-abc")
    token = events.current_emitter.set(em)
    try:
        assert current_task_id() == "task-abc"
    finally:
        events.current_emitter.reset(token)
