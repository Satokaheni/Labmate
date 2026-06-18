from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

pytestmark = [pytest.mark.mocked, pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Task 2 — episode outbox tests
# ---------------------------------------------------------------------------

async def test_store_episode_single_atomic_mongo_write(storage, mock_mongo):
    await storage.store_episode("s1", "hello", {"role": "user"})
    episodes = mock_mongo._collections["episodes"]
    # exactly one MongoDB write, carrying the outbox marker
    assert episodes.insert_one.await_count == 1
    doc = episodes.insert_one.await_args.args[0]
    assert doc["outbox"]["processed"] is False
    assert doc["outbox"]["kind"] == "episode_vector"
    assert doc["content"] == "hello"


async def test_store_episode_does_not_write_chroma_or_redis(storage, mock_chroma, mock_redis):
    await storage.store_episode("s1", "hello", {})
    # outbox pattern: no direct projection from the write path
    mock_chroma.get_or_create_collection.assert_not_awaited()
    mock_redis.xadd.assert_not_awaited()


# ---------------------------------------------------------------------------
# Task 3 — memory write, search filtering, cache, queue
# ---------------------------------------------------------------------------

async def test_store_memory_outbox_marker(storage, mock_mongo):
    await storage.store_memory({"session_id": "s1", "fact": "user prefers dark mode"})
    mem = mock_mongo._collections["memories"]
    assert mem.insert_one.await_count == 1
    doc = mem.insert_one.await_args.args[0]
    assert doc["outbox"]["processed"] is False
    assert doc["valid_to"] is None  # open interval


async def test_search_memories_skips_closed_facts(storage, mock_chroma):
    mock_chroma._collection.query.return_value = {
        "ids": [["a", "b"]],
        "documents": [["current", "stale"]],
        "metadatas": [[{"valid_to": None}, {"valid_to": "2026-01-01T00:00:00Z"}]],
        "distances": [[0.1, 0.2]],
    }
    res = await storage.search_memories("q", top_k=5)
    assert [r["fact"] for r in res] == ["current"]


async def test_enqueue_task_uses_xadd_not_rpush(storage, mock_redis):
    await storage.enqueue_task("tasks", {"type": "consolidate", "session_id": "s1"})
    mock_redis.xadd.assert_awaited_once()
    assert not hasattr(mock_redis, "rpush") or mock_redis.rpush.await_count == 0


async def test_cache_set_get_roundtrip(storage, mock_redis):
    await storage.cache_set("k", "v", ttl=60)
    mock_redis.set.assert_awaited_once()
    args, kwargs = mock_redis.set.await_args
    assert kwargs.get("ex") == 60
    mock_redis.get.return_value = b"v"
    assert await storage.cache_get("k") == "v"


# ---------------------------------------------------------------------------
# Task 4 — OutboxWorker
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def limit(self, _n):
        return self

    def __aiter__(self):
        async def gen():
            for d in self._docs:
                yield d
        return gen()


async def test_outbox_worker_projects_and_marks_processed(storage, mock_mongo, mock_chroma, mock_redis):
    from services.orchestrator.outbox_worker import OutboxWorker

    ep = mock_mongo._collections.setdefault(
        "episodes",
        __import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock(),
    )
    mem = mock_mongo._collections.setdefault(
        "memories",
        __import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock(),
    )
    ep.find = lambda q: _FakeCursor([{
        "_id": "e1",
        "session_id": "s1",
        "content": "hi",
        "outbox": {"kind": "episode_vector"},
    }])
    mem.find = lambda q: _FakeCursor([])

    worker = OutboxWorker(storage)
    handled = await worker.process_once()

    assert handled == 1
    mock_chroma._collection.upsert.assert_awaited_once()
    # point id == Mongo _id (idempotency)
    assert mock_chroma._collection.upsert.await_args.kwargs["ids"] == ["e1"]
    mock_redis.xadd.assert_awaited()         # projected to tasks stream
    ep.update_one.assert_awaited_once()      # marked processed


# ---------------------------------------------------------------------------
# Task 5 — StorageManager async context manager
# ---------------------------------------------------------------------------

async def test_context_manager_starts_outbox_worker():
    """__aenter__ must create and start the OutboxWorker background task."""
    from services.orchestrator.storage_manager import StorageManager
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_mongo = MagicMock()
    mock_chroma = AsyncMock()
    mock_redis = AsyncMock()

    storage = StorageManager.from_clients(
        mongo=mock_mongo, chroma=mock_chroma, redis=mock_redis
    )

    # Mock asyncio.create_task to avoid actually running the background task
    with patch("asyncio.create_task") as mock_create_task:
        mock_task = MagicMock()
        mock_create_task.return_value = mock_task

        result = await storage.__aenter__()

        # __aenter__ should return self
        assert result is storage
        # OutboxWorker should be created
        assert hasattr(storage, "_outbox_worker")
        # create_task should be called with the worker.run() coroutine
        mock_create_task.assert_called_once()
        args, kwargs = mock_create_task.call_args
        assert kwargs.get("name") == "outbox-worker"
        # The task should be stored
        assert storage._outbox_task is mock_task


async def test_context_manager_cancels_outbox_on_exit(mock_mongo, mock_redis):
    """__aexit__ must cancel the outbox worker background task."""
    from services.orchestrator.storage_manager import StorageManager
    from unittest.mock import AsyncMock, MagicMock
    import asyncio

    mock_chroma = AsyncMock()

    storage = StorageManager.from_clients(
        mongo=mock_mongo, chroma=mock_chroma, redis=mock_redis
    )

    # Create a real task that we can cancel and await
    async def dummy_coro():
        pass

    mock_task = asyncio.create_task(dummy_coro())
    storage._outbox_task = mock_task

    await storage.__aexit__(None, None, None)

    # The task should be cancelled
    assert mock_task.cancelled()
    # Redis and Mongo should be closed
    mock_redis.aclose.assert_awaited_once()
    mock_mongo.close.assert_called_once()


async def test_context_manager_closes_connections_on_exit(mock_mongo, mock_redis):
    """__aexit__ must close Redis and Mongo connections."""
    from services.orchestrator.storage_manager import StorageManager
    from unittest.mock import AsyncMock, MagicMock

    mock_chroma = AsyncMock()
    mock_mongo_with_close = MagicMock()
    mock_mongo_with_close.close = MagicMock()

    storage = StorageManager.from_clients(
        mongo=mock_mongo_with_close, chroma=mock_chroma, redis=mock_redis
    )

    # No outbox task in this case
    await storage.__aexit__(None, None, None)

    # Redis should be closed
    mock_redis.aclose.assert_awaited_once()
    # Mongo should be closed
    mock_mongo_with_close.close.assert_called_once()


async def test_full_context_manager_usage_works(mock_mongo, mock_chroma, mock_redis):
    """async with StorageManager() should work without crashing."""
    from services.orchestrator.storage_manager import StorageManager
    from unittest.mock import patch, AsyncMock
    import asyncio

    # Build storage with mocks
    storage = StorageManager.from_clients(
        mongo=mock_mongo, chroma=mock_chroma, redis=mock_redis
    )

    # Patch asyncio.create_task to return a cancellable task
    original_create_task = asyncio.create_task

    def mock_create_task(coro, **kwargs):
        # We need a real task that can be cancelled and awaited
        # Create a task that will run until cancelled
        async def wrapper():
            try:
                await coro
            except asyncio.CancelledError:
                pass
        return original_create_task(wrapper())

    with patch("services.orchestrator.storage_manager.asyncio.create_task", side_effect=mock_create_task):
        # This should not crash
        async with storage as sm:
            assert sm is storage
            assert hasattr(storage, "_outbox_worker")
            assert hasattr(storage, "_outbox_task")


async def test_from_clients_works_without_context_manager():
    """from_clients path should not require async context manager entry."""
    from services.orchestrator.storage_manager import StorageManager
    from unittest.mock import AsyncMock, MagicMock

    mock_mongo = MagicMock()
    mock_chroma = AsyncMock()
    mock_redis = AsyncMock()

    # This should work without entering the context manager
    storage = StorageManager.from_clients(
        mongo=mock_mongo, chroma=mock_chroma, redis=mock_redis
    )

    # Should be able to call methods directly
    assert storage is not None
    # The outbox worker should not be created yet
    assert not hasattr(storage, "_outbox_worker")
    assert not hasattr(storage, "_outbox_task")
