import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from bson import ObjectId


def _make_motor_mock():
    col = MagicMock()
    col.create_index = AsyncMock()
    col.watch = MagicMock()
    db = MagicMock()
    db.messages = col
    db.tool_calls = MagicMock()
    db.tool_calls.create_index = AsyncMock()
    db.sessions = MagicMock()
    db.sessions.create_index = AsyncMock()
    db.meta = MagicMock()
    db.meta.find_one = AsyncMock(return_value=None)
    db.meta.update_one = AsyncMock()
    client = MagicMock()
    client.labmate = db
    client.close = MagicMock()
    return client, db


def _make_chroma_mock():
    col = AsyncMock()
    client = AsyncMock()
    client.get_or_create_collection = AsyncMock(return_value=col)
    return client, col


def _make_redis_mock():
    pool = MagicMock()
    redis = AsyncMock()
    redis.aclose = AsyncMock()
    return pool, redis


@pytest.mark.asyncio
async def test_storage_manager_enters_and_exits_cleanly():
    motor_client, db = _make_motor_mock()
    chroma_client, chroma_col = _make_chroma_mock()
    redis_pool, redis_client = _make_redis_mock()

    with (
        patch("services.memory.storage_manager.AsyncIOMotorClient", return_value=motor_client),
        patch("services.memory.storage_manager.chromadb.AsyncHttpClient", new_callable=AsyncMock, return_value=chroma_client),
        patch("services.memory.storage_manager.aioredis.ConnectionPool.from_url", return_value=redis_pool),
        patch("services.memory.storage_manager.aioredis.Redis", return_value=redis_client),
    ):
        from services.memory.storage_manager import StorageManager
        async with StorageManager("mongodb://localhost:27017", "localhost", 8000, "redis://localhost:6379") as sm:
            assert sm.db is db
            assert sm.redis is redis_client

        motor_client.close.assert_called_once()
        redis_client.aclose.assert_called_once()


@pytest.mark.asyncio
async def test_ensure_indexes_called_on_enter():
    motor_client, db = _make_motor_mock()
    chroma_client, chroma_col = _make_chroma_mock()
    redis_pool, redis_client = _make_redis_mock()

    with (
        patch("services.memory.storage_manager.AsyncIOMotorClient", return_value=motor_client),
        patch("services.memory.storage_manager.chromadb.AsyncHttpClient", new_callable=AsyncMock, return_value=chroma_client),
        patch("services.memory.storage_manager.aioredis.ConnectionPool.from_url", return_value=redis_pool),
        patch("services.memory.storage_manager.aioredis.Redis", return_value=redis_client),
    ):
        from services.memory.storage_manager import StorageManager
        async with StorageManager("mongodb://localhost:27017", "localhost", 8000, "redis://localhost:6379") as sm:
            assert db.messages.create_index.call_count >= 3
            assert db.sessions.create_index.call_count >= 1


@pytest.mark.asyncio
async def test_write_message_inserts_with_outbox_marker():
    """write_message inserts ONE document atomically with outbox.processed=False."""
    motor_client, db = _make_motor_mock()
    chroma_client, chroma_col = _make_chroma_mock()
    redis_pool, redis_client = _make_redis_mock()

    inserted_doc = {}
    async def capture_insert(doc):
        inserted_doc.update(doc)
        return MagicMock(inserted_id=doc["_id"])
    db.messages.insert_one = AsyncMock(side_effect=capture_insert)

    with (
        patch("services.memory.storage_manager.AsyncIOMotorClient", return_value=motor_client),
        patch("services.memory.storage_manager.chromadb.AsyncHttpClient", new_callable=AsyncMock, return_value=chroma_client),
        patch("services.memory.storage_manager.aioredis.ConnectionPool.from_url", return_value=redis_pool),
        patch("services.memory.storage_manager.aioredis.Redis", return_value=redis_client),
    ):
        from services.memory.storage_manager import StorageManager
        async with StorageManager("mongodb://localhost:27017", "localhost", 8000, "redis://localhost:6379") as sm:
            embedding = [0.1, 0.2, 0.3]
            msg_id = await sm.write_message(
                session_id="sess-1",
                seq=1,
                role="assistant",
                content="hello",
                embedding=embedding,
                importance=0.7,
            )

    # Exactly one insert_one call
    db.messages.insert_one.assert_called_once()
    assert inserted_doc["session_id"] == "sess-1"
    assert inserted_doc["seq"] == 1
    assert inserted_doc["role"] == "assistant"
    assert inserted_doc["content"] == "hello"
    assert inserted_doc["outbox"]["processed"] is False
    assert inserted_doc["outbox"]["embedding"] == embedding
    assert inserted_doc["outbox"]["kind"] == "vector"
    assert inserted_doc["importance"] == 0.7
    assert msg_id == inserted_doc["_id"]


@pytest.mark.asyncio
async def test_write_message_returns_object_id():
    from bson import ObjectId
    motor_client, db = _make_motor_mock()
    chroma_client, chroma_col = _make_chroma_mock()
    redis_pool, redis_client = _make_redis_mock()
    db.messages.insert_one = AsyncMock(return_value=MagicMock())

    with (
        patch("services.memory.storage_manager.AsyncIOMotorClient", return_value=motor_client),
        patch("services.memory.storage_manager.chromadb.AsyncHttpClient", new_callable=AsyncMock, return_value=chroma_client),
        patch("services.memory.storage_manager.aioredis.ConnectionPool.from_url", return_value=redis_pool),
        patch("services.memory.storage_manager.aioredis.Redis", return_value=redis_client),
    ):
        from services.memory.storage_manager import StorageManager
        async with StorageManager("mongodb://localhost:27017", "localhost", 8000, "redis://localhost:6379") as sm:
            msg_id = await sm.write_message("s", 1, "user", "hi", [0.0], 0.5)
            assert isinstance(msg_id, ObjectId)


@pytest.mark.asyncio
async def test_search_memory_resolves_chroma_ids_to_mongo_docs():
    from bson import ObjectId
    motor_client, db = _make_motor_mock()
    chroma_client, chroma_col = _make_chroma_mock()
    redis_pool, redis_client = _make_redis_mock()

    oid = ObjectId()
    chroma_col.query = AsyncMock(return_value={
        "ids": [[str(oid)]],
        "distances": [[0.1]],
    })

    mongo_doc = {"_id": oid, "session_id": "s1", "content": "hello", "seq": 1}

    class AsyncDocIter:
        def __init__(self, docs):
            self._docs = iter(docs)
        def __aiter__(self): return self
        async def __anext__(self):
            try:
                return next(self._docs)
            except StopIteration:
                raise StopAsyncIteration

    db.messages.find = MagicMock(return_value=AsyncDocIter([mongo_doc]))

    with (
        patch("services.memory.storage_manager.AsyncIOMotorClient", return_value=motor_client),
        patch("services.memory.storage_manager.chromadb.AsyncHttpClient", new_callable=AsyncMock, return_value=chroma_client),
        patch("services.memory.storage_manager.aioredis.ConnectionPool.from_url", return_value=redis_pool),
        patch("services.memory.storage_manager.aioredis.Redis", return_value=redis_client),
    ):
        from services.memory.storage_manager import StorageManager
        async with StorageManager("mongodb://localhost:27017", "localhost", 8000, "redis://localhost:6379") as sm:
            sm.vectors["semantic"] = chroma_col
            results = await sm.search_memory([0.1, 0.2], collection="semantic", k=5)

    assert len(results) == 1
    assert results[0]["content"] == "hello"
    assert "_similarity" in results[0]
    assert abs(results[0]["_similarity"] - 0.9) < 1e-9


@pytest.mark.asyncio
async def test_enqueue_task_calls_xadd():
    motor_client, db = _make_motor_mock()
    chroma_client, chroma_col = _make_chroma_mock()
    redis_pool, redis_client = _make_redis_mock()
    redis_client.xadd = AsyncMock(return_value="1234567890-0")

    with (
        patch("services.memory.storage_manager.AsyncIOMotorClient", return_value=motor_client),
        patch("services.memory.storage_manager.chromadb.AsyncHttpClient", new_callable=AsyncMock, return_value=chroma_client),
        patch("services.memory.storage_manager.aioredis.ConnectionPool.from_url", return_value=redis_pool),
        patch("services.memory.storage_manager.aioredis.Redis", return_value=redis_client),
    ):
        from services.memory.storage_manager import StorageManager
        async with StorageManager("mongodb://localhost:27017", "localhost", 8000, "redis://localhost:6379") as sm:
            entry_id = await sm.enqueue_task(
                "tasks",
                {"msg_id": "abc", "session_id": "s1", "kind": "consolidate"},
            )

    assert entry_id == "1234567890-0"
    redis_client.xadd.assert_called_once_with(
        "tasks",
        {"msg_id": "abc", "session_id": "s1", "kind": "consolidate"},
        maxlen=10_000,
        approximate=True,
    )


@pytest.mark.asyncio
async def test_outbox_worker_projects_to_chroma_and_marks_processed():
    """_run_outbox upserts to Chroma, enqueues to Redis, marks outbox.processed=True."""
    from bson import ObjectId
    motor_client, db = _make_motor_mock()
    chroma_client, chroma_col = _make_chroma_mock()
    redis_pool, redis_client = _make_redis_mock()
    redis_client.xadd = AsyncMock(return_value="1-0")

    oid = ObjectId()
    change_event = {
        "_id": {"token": "tok123"},
        "fullDocument": {
            "_id": oid,
            "session_id": "s1",
            "seq": 1,
            "content": "test content",
            "importance": 0.6,
            "outbox": {
                "embedding": [0.1, 0.2, 0.3],
                "processed": False,
            },
        },
    }

    class FakeChangeStream:
        def __init__(self):
            self._events = [change_event]
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        def __aiter__(self):
            return self
        async def __anext__(self):
            if self._events:
                return self._events.pop(0)
            await asyncio.sleep(9999)  # hang — task will be cancelled

    db.messages.watch = MagicMock(return_value=FakeChangeStream())
    db.messages.update_one = AsyncMock()
    db.meta.find_one = AsyncMock(return_value=None)
    db.meta.update_one = AsyncMock()

    with (
        patch("services.memory.storage_manager.AsyncIOMotorClient", return_value=motor_client),
        patch("services.memory.storage_manager.chromadb.AsyncHttpClient", new_callable=AsyncMock, return_value=chroma_client),
        patch("services.memory.storage_manager.aioredis.ConnectionPool.from_url", return_value=redis_pool),
        patch("services.memory.storage_manager.aioredis.Redis", return_value=redis_client),
    ):
        from services.memory.storage_manager import StorageManager
        sm = StorageManager("mongodb://localhost:27017", "localhost", 8000, "redis://localhost:6379")
        async with sm:
            sm.vectors["episodic"] = chroma_col
            # Give the outbox task time to process the one event
            await asyncio.sleep(0.2)

    # Chroma upsert called
    chroma_col.upsert.assert_called_once()
    upsert_call = chroma_col.upsert.call_args
    # ids is a keyword arg
    upsert_ids = upsert_call.kwargs.get("ids") or upsert_call.args[0]
    assert str(oid) in upsert_ids

    # Redis task enqueued
    redis_client.xadd.assert_called()

    # MongoDB marked processed
    db.messages.update_one.assert_called_once()
    update_call = db.messages.update_one.call_args
    set_doc = update_call[0][1]["$set"]
    assert set_doc["outbox.processed"] is True
