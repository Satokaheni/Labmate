from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

pytestmark = [pytest.mark.asyncio]


def _make_cm(storage):
    from services.memory.context_manager import ContextManager
    redis = AsyncMock()
    redis.get = AsyncMock(return_value="")
    cm = ContextManager(
        redis=redis,
        mongo_db=MagicMock(),
        chroma_cols={},
        embedder=AsyncMock(return_value=[[0.0, 0.1]]),
        storage=storage,
    )
    return cm


async def test_boost_retrieved_calls_storage_per_chunk():
    storage = AsyncMock()
    cm = _make_cm(storage)
    await cm._boost_retrieved([{"id": "a", "text": "x"}, {"id": "b", "text": "y"}])
    assert storage.boost_memory_importance.await_count == 2
    storage.boost_memory_importance.assert_any_await("a", delta=0.1)
    storage.boost_memory_importance.assert_any_await("b", delta=0.1)


async def test_boost_retrieved_noop_without_storage():
    from services.memory.context_manager import ContextManager
    cm = ContextManager(
        redis=AsyncMock(), mongo_db=MagicMock(), chroma_cols={},
        embedder=AsyncMock(),
    )
    # no storage hook -> must not raise
    await cm._boost_retrieved([{"id": "a", "text": "x"}])


async def test_boost_retrieved_swallows_errors():
    storage = AsyncMock()
    storage.boost_memory_importance = AsyncMock(side_effect=RuntimeError("boom"))
    cm = _make_cm(storage)
    # error inside boost must not propagate
    await cm._boost_retrieved([{"id": "a", "text": "x"}])
