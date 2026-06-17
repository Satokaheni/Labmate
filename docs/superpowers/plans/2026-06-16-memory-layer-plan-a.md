# Memory Layer Plan A — StorageManager + ContextManager

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the persistence and context-assembly layer: `StorageManager` (Motor + Chroma + Redis + transactional outbox) and `ContextManager` (Gemma token budget + `build_context`).

**Architecture:** `StorageManager` is an async context manager owning one shared client of each storage backend. `write_message()` inserts atomically with an outbox marker; a background task tails the MongoDB change stream and projects vectors to Chroma. `ContextManager` assembles the per-step prompt strictly within a Gemma-tokenized budget. Embedding is injected as a dependency so it can be mocked in tests without GPU.

**Tech Stack:** Python 3.11+, motor 3.5+, chromadb 0.5+ (AsyncHttpClient), redis.asyncio 5+, transformers AutoTokenizer (Gemma SentencePiece), pydantic 2+, pytest, pytest-asyncio

**Plan B (deferred):** `hybrid_retrieve()` BM25+dense+RRF+FlagReranker, consolidation worker — both require running embedding models.

---

## File Map

| File | Responsibility |
|---|---|
| `services/memory/__init__.py` | Package marker |
| `services/memory/tokenizer.py` | `token_count()` singleton — Gemma AutoTokenizer, never tiktoken |
| `services/memory/storage_manager.py` | `StorageManager` — clients, `write_message`, `search_memory`, `enqueue_task`, `_run_outbox` |
| `services/memory/context_manager.py` | `ContextBudget`, `AssembledContext`, `ContextManager.build_context` |
| `services/memory/requirements.txt` | Pinned Python deps |
| `services/memory/tests/__init__.py` | Package marker |
| `services/memory/tests/test_tokenizer.py` | Token count unit tests |
| `services/memory/tests/test_storage_manager.py` | StorageManager unit tests (all clients mocked) |
| `services/memory/tests/test_context_manager.py` | ContextManager unit tests (mocked Redis, Mongo, embed) |

---

## Task 1: Project Scaffold

**Files:**
- Create: `services/memory/__init__.py`
- Create: `services/memory/tests/__init__.py`
- Create: `services/memory/requirements.txt`

- [ ] **Step 1: Create directories**

```bash
mkdir -p /Users/zachstallbohm/Work/gemma/services/memory/tests
```

- [ ] **Step 2: Create `services/memory/__init__.py`** (empty)

```python
```

- [ ] **Step 3: Create `services/memory/tests/__init__.py`** (empty)

```python
```

- [ ] **Step 4: Create `services/memory/requirements.txt`**

```
motor>=3.5
pymongo>=4.9
chromadb>=0.5
redis>=5.0
transformers>=4.40
pydantic>=2.0
bson>=0.5
pytest>=8.0
pytest-asyncio>=0.23
unittest-mock>=1.0; python_version < "3.8"
```

- [ ] **Step 5: Install deps**

```bash
cd /Users/zachstallbohm/Work/gemma/services/memory && pip install -r requirements.txt
```

Expected: installs without errors. `transformers` will pull in torch — this is expected.

- [ ] **Step 6: Commit**

```bash
cd /Users/zachstallbohm/Work/gemma && git add services/memory/ && git commit -m "feat(memory): Python package scaffold"
```

---

## Task 2: Tokenizer Module

**Files:**
- Create: `services/memory/tokenizer.py`
- Create: `services/memory/tests/test_tokenizer.py`

**Critical:** Use `google/gemma-4-9b-it` AutoTokenizer (SentencePiece). Never tiktoken. The tokenizer is a lazy module-level singleton — loaded once, thread-safe for encoding. In tests, the singleton is patched to avoid a model download.

- [ ] **Step 1: Create `tests/test_tokenizer.py` first**

```python
import pytest
from unittest.mock import patch, MagicMock


def _make_mock_tokenizer(chars_per_token: int = 4):
    """Returns a tokenizer mock where encode() approximates 1 token per N chars."""
    tok = MagicMock()
    tok.encode.side_effect = lambda text, **kw: list(range(max(1, len(text) // chars_per_token)))
    return tok


def test_token_count_empty_string():
    mock_tok = _make_mock_tokenizer()
    with patch("services.memory.tokenizer._TOKENIZER", mock_tok):
        from services.memory.tokenizer import token_count
        assert token_count("") == 0


def test_token_count_nonempty():
    mock_tok = _make_mock_tokenizer(chars_per_token=4)
    with patch("services.memory.tokenizer._TOKENIZER", mock_tok):
        from services.memory.tokenizer import token_count
        result = token_count("hello world!")  # 12 chars → 3 tokens
        assert result == 3


def test_token_count_delegates_to_tokenizer():
    mock_tok = _make_mock_tokenizer()
    with patch("services.memory.tokenizer._TOKENIZER", mock_tok):
        from services.memory.tokenizer import token_count
        token_count("some text")
        mock_tok.encode.assert_called_once_with(
            "some text", add_special_tokens=False
        )
```

- [ ] **Step 2: Run test to confirm FAIL**

```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest services/memory/tests/test_tokenizer.py -v 2>&1 | tail -10
```

Expected: ImportError — module not found.

- [ ] **Step 3: Create `services/memory/tokenizer.py`**

```python
from __future__ import annotations
from transformers import AutoTokenizer

# Gemma SentencePiece tokenizer — loaded once at module import, reused forever.
# NEVER use tiktoken: it uses GPT BPE and miscounts Gemma tokens by 30%+ on code.
# Token count errors cause context-window overflows or under-fills.
_TOKENIZER = AutoTokenizer.from_pretrained(
    "google/gemma-4-9b-it",
    use_fast=True,
)


def token_count(text: str) -> int:
    """Count tokens with the Gemma AutoTokenizer (SentencePiece)."""
    if not text:
        return 0
    return len(_TOKENIZER.encode(text, add_special_tokens=False))
```

- [ ] **Step 4: Run tests from repo root (sys.path includes services/)**

```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest services/memory/tests/test_tokenizer.py -v
```

Expected: 3 tests pass. If `AutoTokenizer.from_pretrained` errors on import (gated model, no HF token), set the env var:
```bash
TRANSFORMERS_OFFLINE=1 python -m pytest services/memory/tests/test_tokenizer.py -v
```
The mock patches `_TOKENIZER` before `token_count` runs, so the actual tokenizer load can be bypassed with `--import-mode=importlib` or by pre-patching at collection time if needed.

If the tokenizer cannot be loaded (gated model), update `tokenizer.py` to use a lazy loader:

```python
from __future__ import annotations
from functools import lru_cache
from transformers import AutoTokenizer

@lru_cache(maxsize=1)
def _get_tokenizer():
    return AutoTokenizer.from_pretrained("google/gemma-4-9b-it", use_fast=True)

# Module-level alias — tests patch this name directly
_TOKENIZER = None  # populated on first call if not patched

def token_count(text: str) -> int:
    if not text:
        return 0
    tok = _TOKENIZER if _TOKENIZER is not None else _get_tokenizer()
    return len(tok.encode(text, add_special_tokens=False))
```

Use whichever version makes the tests pass. The lazy version avoids import-time model loading.

- [ ] **Step 5: Confirm all 3 tests pass**

```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest services/memory/tests/test_tokenizer.py -v
```

- [ ] **Step 6: Commit**

```bash
cd /Users/zachstallbohm/Work/gemma && git add services/memory/tokenizer.py services/memory/tests/test_tokenizer.py && git commit -m "feat(memory): Gemma AutoTokenizer token_count (never tiktoken)"
```

---

## Task 3: StorageManager — Clients + Indexes

**Files:**
- Create: `services/memory/storage_manager.py` (partial — client lifecycle + `_ensure_indexes`)
- Create: `services/memory/tests/test_storage_manager.py` (partial)

This task covers only the async context manager skeleton and index creation. `write_message`, `search_memory`, `enqueue_task`, and the outbox worker are added in Tasks 4–6.

- [ ] **Step 1: Write failing tests for client setup**

Create `services/memory/tests/test_storage_manager.py`:

```python
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call


def _make_motor_mock():
    col = MagicMock()
    col.create_index = AsyncMock()
    db = MagicMock()
    db.messages = col
    db.tool_calls = MagicMock()
    db.tool_calls.create_index = AsyncMock()
    db.sessions = MagicMock()
    db.sessions.create_index = AsyncMock()
    db.meta = MagicMock()
    db.meta.find_one = AsyncMock(return_value=None)
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
    redis_client.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))

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
```

- [ ] **Step 2: Run tests to confirm FAIL**

```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest services/memory/tests/test_storage_manager.py -v 2>&1 | tail -10
```

- [ ] **Step 3: Create `services/memory/storage_manager.py`** (client lifecycle + indexes only)

```python
from __future__ import annotations

import asyncio
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient
import chromadb
import redis.asyncio as aioredis

EMBED_MODEL = "BAAI/bge-small-en-v1.5"


class StorageManager:
    """Async context manager owning one shared client of each storage backend.

    Usage:
        async with StorageManager(mongo_uri, chroma_host, chroma_port, redis_url) as sm:
            msg_id = await sm.write_message(...)
    """

    def __init__(
        self,
        mongo_uri: str,
        chroma_host: str,
        chroma_port: int,
        redis_url: str,
    ) -> None:
        self._mongo_uri    = mongo_uri
        self._chroma_host  = chroma_host
        self._chroma_port  = chroma_port
        self._redis_url    = redis_url
        self._outbox_task: asyncio.Task | None = None

    async def __aenter__(self) -> "StorageManager":
        self.mongo = AsyncIOMotorClient(
            self._mongo_uri,
            maxPoolSize=50,
            minPoolSize=5,
            waitQueueTimeoutMS=5_000,
            serverSelectionTimeoutMS=5_000,
            connectTimeoutMS=5_000,
            socketTimeoutMS=30_000,
        )
        self.db = self.mongo.labmate

        self.chroma = await chromadb.AsyncHttpClient(
            host=self._chroma_host,
            port=self._chroma_port,
        )
        self.vectors: dict = {
            col: await self.chroma.get_or_create_collection(
                col,
                metadata={"embed_model": EMBED_MODEL, "hnsw:space": "cosine"},
            )
            for col in ("episodic", "semantic", "procedural")
        }

        pool = aioredis.ConnectionPool.from_url(
            self._redis_url,
            max_connections=50,
            decode_responses=True,
        )
        self.redis = aioredis.Redis(connection_pool=pool)

        await self._ensure_indexes()
        self._outbox_task = asyncio.create_task(self._run_outbox())
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._outbox_task:
            self._outbox_task.cancel()
            try:
                await self._outbox_task
            except asyncio.CancelledError:
                pass
        self.mongo.close()
        await self.redis.aclose()

    async def _ensure_indexes(self) -> None:
        """Idempotent index creation — safe to call on every startup."""
        await self.db.messages.create_index(
            [("session_id", 1), ("seq", 1)], unique=True
        )
        await self.db.messages.create_index(
            [("session_id", 1), ("created_at", -1)]
        )
        await self.db.messages.create_index([("outbox.processed", 1)])
        await self.db.tool_calls.create_index(
            [("session_id", 1), ("message_seq", 1)]
        )
        await self.db.tool_calls.create_index([("outbox.processed", 1)])
        await self.db.sessions.create_index("expire_at", expireAfterSeconds=0)

    # Stubs filled in subsequent tasks
    async def write_message(self, *args, **kwargs):
        raise NotImplementedError

    async def search_memory(self, *args, **kwargs):
        raise NotImplementedError

    async def enqueue_task(self, *args, **kwargs):
        raise NotImplementedError

    async def _run_outbox(self) -> None:
        raise NotImplementedError
```

- [ ] **Step 4: Run tests to confirm PASS**

```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest services/memory/tests/test_storage_manager.py -v
```

Expected: 2 tests pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/zachstallbohm/Work/gemma && git add services/memory/storage_manager.py services/memory/tests/test_storage_manager.py && git commit -m "feat(memory): StorageManager client lifecycle + index creation"
```

---

## Task 4: `write_message()` with Transactional Outbox

**Files:**
- Modify: `services/memory/storage_manager.py` — implement `write_message`
- Modify: `services/memory/tests/test_storage_manager.py` — add tests

- [ ] **Step 1: Add tests for `write_message`**

Append to `services/memory/tests/test_storage_manager.py`:

```python
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
```

- [ ] **Step 2: Run tests to confirm the new ones FAIL**

```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest services/memory/tests/test_storage_manager.py::test_write_message_inserts_with_outbox_marker -v 2>&1 | tail -5
```

Expected: FAIL — `NotImplementedError`.

- [ ] **Step 3: Implement `write_message` in `storage_manager.py`**

Replace the `write_message` stub:

```python
async def write_message(
    self,
    session_id: str,
    seq: int,
    role: str,
    content: str,
    embedding: list[float],
    importance: float = 0.5,
) -> ObjectId:
    """Insert message + outbox marker in ONE atomic MongoDB write.

    The outbox worker picks up the marker and projects the vector to Chroma.
    Cross-store atomicity is guaranteed by this single-document write.
    """
    import time
    _id = ObjectId()
    await self.db.messages.insert_one({
        "_id":        _id,
        "session_id": session_id,
        "seq":        seq,
        "role":       role,
        "content":    content,
        "created_at": time.time(),
        "importance": importance,
        "outbox": {
            "kind":         "vector",
            "embedding":    embedding,
            "processed":    False,
            "processed_at": None,
        },
    })
    return _id
```

- [ ] **Step 4: Run all storage manager tests**

```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest services/memory/tests/test_storage_manager.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/zachstallbohm/Work/gemma && git add services/memory/storage_manager.py services/memory/tests/test_storage_manager.py && git commit -m "feat(memory): write_message with transactional outbox marker"
```

---

## Task 5: `search_memory()` and `enqueue_task()`

**Files:**
- Modify: `services/memory/storage_manager.py`
- Modify: `services/memory/tests/test_storage_manager.py`

- [ ] **Step 1: Add tests**

Append to `test_storage_manager.py`:

```python
@pytest.mark.asyncio
async def test_search_memory_resolves_chroma_ids_to_mongo_docs():
    motor_client, db = _make_motor_mock()
    chroma_client, chroma_col = _make_chroma_mock()
    redis_pool, redis_client = _make_redis_mock()

    oid = ObjectId()
    chroma_col.query = AsyncMock(return_value={
        "ids": [[str(oid)]],
        "distances": [[0.1]],
    })

    mongo_doc = {"_id": oid, "session_id": "s1", "content": "hello", "seq": 1}

    async def fake_find(query):
        class FakeCursor:
            def __aiter__(self):
                return self
            async def __anext__(self):
                raise StopAsyncIteration
        # yield the doc if _id matches
        class RealCursor:
            def __init__(self):
                self._done = False
            def __aiter__(self):
                return self
            async def __anext__(self):
                if not self._done:
                    self._done = True
                    return mongo_doc
                raise StopAsyncIteration
        return RealCursor()

    db.messages.find = MagicMock(return_value=fake_find({}))

    # Simplify: just mock find to return an async iterable
    class AsyncDocIter:
        def __init__(self, docs):
            self._docs = iter(docs)
        def __aiter__(self):
            return self
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
            # Point sm.vectors["semantic"] at our mock
            sm.vectors["semantic"] = chroma_col
            results = await sm.search_memory([0.1, 0.2], collection="semantic", k=5)

    assert len(results) == 1
    assert results[0]["content"] == "hello"
    assert "_similarity" in results[0]
    assert results[0]["_similarity"] == pytest.approx(0.9)


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
```

- [ ] **Step 2: Run new tests to confirm FAIL**

```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest services/memory/tests/test_storage_manager.py::test_search_memory_resolves_chroma_ids_to_mongo_docs services/memory/tests/test_storage_manager.py::test_enqueue_task_calls_xadd -v 2>&1 | tail -5
```

- [ ] **Step 3: Implement `search_memory` and `enqueue_task` in `storage_manager.py`**

Replace the two stubs:

```python
async def search_memory(
    self,
    query_embedding: list[float],
    collection: str = "semantic",
    k: int = 10,
    where: dict | None = None,
) -> list[dict]:
    """Vector search in Chroma, resolved to full MongoDB records.

    MongoDB _id IS the Chroma point ID — results always consistent with
    source of truth. Orphan vectors (stale Chroma points with no matching
    MongoDB doc) are silently dropped.
    """
    col = self.vectors[collection]
    kwargs: dict = dict(
        query_embeddings=[query_embedding],
        n_results=k,
        include=["metadatas", "distances"],
    )
    if where:
        kwargs["where"] = where
    res = await col.query(**kwargs)

    chroma_ids = res["ids"][0]
    if not chroma_ids:
        return []

    object_ids = [ObjectId(cid) for cid in chroma_ids]
    cursor = self.db.messages.find({"_id": {"$in": object_ids}})
    docs = {doc["_id"]: doc async for doc in cursor}

    results = []
    for cid, dist in zip(chroma_ids, res["distances"][0]):
        oid = ObjectId(cid)
        if oid not in docs:
            continue  # orphan vector — skip
        doc = docs[oid]
        doc["_similarity"] = 1.0 - dist
        results.append(doc)
    return results

async def enqueue_task(
    self,
    stream: str,
    fields: dict,
    maxlen: int = 10_000,
) -> str:
    """Enqueue a task onto a Redis Stream. Returns the entry ID.

    Always use Streams (XADD/XREADGROUP/XACK), never LIST+BRPOP.
    Unacked entries survive crashes and are recovered via XAUTOCLAIM.
    """
    return await self.redis.xadd(stream, fields, maxlen=maxlen, approximate=True)
```

- [ ] **Step 4: Run all storage manager tests**

```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest services/memory/tests/test_storage_manager.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/zachstallbohm/Work/gemma && git add services/memory/storage_manager.py services/memory/tests/test_storage_manager.py && git commit -m "feat(memory): search_memory (Chroma→Mongo resolve) and enqueue_task (XADD)"
```

---

## Task 6: Outbox Worker (`_run_outbox`)

**Files:**
- Modify: `services/memory/storage_manager.py`
- Modify: `services/memory/tests/test_storage_manager.py`

- [ ] **Step 1: Add outbox worker tests**

Append to `test_storage_manager.py`:

```python
@pytest.mark.asyncio
async def test_outbox_worker_projects_to_chroma_and_marks_processed():
    """_run_outbox upserts to Chroma, enqueues to Redis, marks outbox.processed=True."""
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

    # Change stream yields one event then hangs (simulating real stream)
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

    # Chroma upsert called with the correct id and embedding
    chroma_col.upsert.assert_called_once()
    upsert_kwargs = chroma_col.upsert.call_args
    assert str(oid) in upsert_kwargs.kwargs.get("ids", upsert_kwargs.args[0] if upsert_kwargs.args else [])

    # Redis task enqueued
    redis_client.xadd.assert_called()

    # MongoDB marked processed
    db.messages.update_one.assert_called_once()
    update_call = db.messages.update_one.call_args
    assert update_call[0][1]["$set"]["outbox.processed"] is True
```

- [ ] **Step 2: Run test to confirm FAIL**

```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest services/memory/tests/test_storage_manager.py::test_outbox_worker_projects_to_chroma_and_marks_processed -v 2>&1 | tail -5
```

Expected: FAIL — `NotImplementedError`.

- [ ] **Step 3: Implement `_run_outbox` in `storage_manager.py`**

Replace the `_run_outbox` stub:

```python
async def _run_outbox(self) -> None:
    """Tail the MongoDB change stream; project unprocessed outbox entries to
    Chroma and Redis. Persists resume token after each event so restarts never
    drop events that occurred during downtime."""
    tok_doc = await self.db.meta.find_one({"_id": "outbox_token"})
    resume_token = tok_doc["token"] if tok_doc else None

    pipeline = [{"$match": {
        "operationType": "insert",
        "fullDocument.outbox.processed": False,
    }}]

    watch_kwargs: dict = dict(
        pipeline=pipeline,
        full_document="updateLookup",
    )
    if resume_token:
        watch_kwargs["resume_after"] = resume_token

    try:
        async with self.db.messages.watch(**watch_kwargs) as stream:
            async for change in stream:
                doc  = change["fullDocument"]
                _id  = doc["_id"]
                emb  = doc["outbox"]["embedding"]

                # Idempotent upsert: MongoDB _id IS the Chroma point ID
                await self.vectors["episodic"].upsert(
                    ids=[str(_id)],
                    embeddings=[emb],
                    documents=[doc["content"]],
                    metadatas=[{
                        "session_id": doc["session_id"],
                        "seq":        doc["seq"],
                        "embed_model": EMBED_MODEL,
                        "importance": doc.get("importance", 0.5),
                    }],
                )

                await self.enqueue_task(
                    "consolidate",
                    {"msg_id": str(_id), "session_id": doc["session_id"]},
                )

                import time
                await self.db.messages.update_one(
                    {"_id": _id},
                    {"$set": {
                        "outbox.processed":    True,
                        "outbox.processed_at": time.time(),
                    }},
                )

                # Persist resume token AFTER successful processing
                await self.db.meta.update_one(
                    {"_id": "outbox_token"},
                    {"$set": {"token": change["_id"]}},
                    upsert=True,
                )
    except asyncio.CancelledError:
        return
```

- [ ] **Step 4: Run all storage manager tests**

```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest services/memory/tests/test_storage_manager.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/zachstallbohm/Work/gemma && git add services/memory/storage_manager.py services/memory/tests/test_storage_manager.py && git commit -m "feat(memory): outbox worker (change stream → Chroma upsert + Redis enqueue)"
```

---

## Task 7: `ContextManager`

**Files:**
- Create: `services/memory/context_manager.py`
- Create: `services/memory/tests/test_context_manager.py`

- [ ] **Step 1: Write failing tests**

Create `services/memory/tests/test_context_manager.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _mock_token_count(text: str) -> int:
    """Deterministic stub: 1 token per 4 chars."""
    return max(0, len(text) // 4)


@pytest.mark.asyncio
async def test_build_context_stays_within_budget():
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager, ContextBudget

        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=lambda key: "goal: finish MCP bridge" if "core" in key else "old summary")
        db = MagicMock()

        class AsyncDocIter:
            def __init__(self, docs):
                self._docs = iter(docs)
            def __aiter__(self): return self
            async def __anext__(self):
                try: return next(self._docs)
                except StopIteration: raise StopAsyncIteration

        cursor = AsyncDocIter([
            {"role": "user", "content": "hello", "seq": 1},
            {"role": "assistant", "content": "hi there", "seq": 2},
        ])
        db.messages.find.return_value = cursor
        db.messages.find.return_value.sort = MagicMock(return_value=cursor)
        db.messages.find.return_value.sort.return_value.limit = MagicMock(return_value=cursor)

        embed = AsyncMock(return_value=[[0.1, 0.2]])

        budget = ContextBudget(max_tokens=200, completion_reserve=20)
        cm = ContextManager(redis=redis, mongo_db=db, chroma_cols={}, embedder=embed, budget=budget)

        ctx = await cm.build_context(
            session_id="s1",
            current_task="implement feature X",
            system_prompt="You are Labmate.",
        )

        assert ctx.total_tokens <= budget.effective_budget
        assert "You are Labmate." in ctx.system_prompt
        assert ctx.core_memory  # should contain pinned goal


@pytest.mark.asyncio
async def test_build_context_pins_core_memory_even_when_over_budget():
    """Core memory is never trimmed — only summary and recent turns are."""
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager, ContextBudget

        redis = AsyncMock()
        # Core memory is 2000 chars → 500 tokens. Budget is only 600 effective.
        long_core = "GOAL: " + "x" * 1994
        redis.get = AsyncMock(side_effect=lambda key: long_core if "core" in key else "")

        db = MagicMock()

        class EmptyCursor:
            def __aiter__(self): return self
            async def __anext__(self): raise StopAsyncIteration

        empty = EmptyCursor()
        db.messages.find.return_value = empty
        db.messages.find.return_value.sort = MagicMock(return_value=empty)
        db.messages.find.return_value.sort.return_value.limit = MagicMock(return_value=empty)

        embed = AsyncMock(return_value=[[0.1]])
        budget = ContextBudget(max_tokens=700, completion_reserve=100)
        cm = ContextManager(redis=redis, mongo_db=db, chroma_cols={}, embedder=embed, budget=budget)

        ctx = await cm.build_context("s1", "task", "system")
        # Core memory is preserved even if it dominates the budget
        assert ctx.core_memory == long_core


@pytest.mark.asyncio
async def test_trim_to_budget_drops_oldest_lines():
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        cm = ContextManager(
            redis=AsyncMock(), mongo_db=MagicMock(),
            chroma_cols={}, embedder=AsyncMock(),
        )
        # 5 lines × ~10 chars each → ~2 tokens each. Budget = 6 → 3 lines fit.
        text = "\n".join([f"line {i} text" for i in range(5)])  # 5 lines, ~13 chars each
        result = cm._trim_to_budget(text, budget=6)
        lines = [l for l in result.splitlines() if l]
        assert len(lines) <= 3
        # Newest lines (highest index) are retained
        assert "line 4 text" in result


def test_context_budget_effective_budget():
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextBudget
        b = ContextBudget(max_tokens=8192, completion_reserve=700)
        assert b.effective_budget == 7492
        assert b.slot(0.25) == int(7492 * 0.25)


def test_assembled_context_as_prompt_ordering():
    """RAG evidence appears before summary, which appears before recent turns."""
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import AssembledContext
        ctx = AssembledContext(
            system_prompt="sys",
            core_memory="goal",
            recent_turns="recent",
            retrieved_context="rag",
            summary_buffer="summary",
        )
        prompt = ctx.as_prompt()
        rag_pos    = prompt.index("rag")
        summary_pos = prompt.index("summary")
        recent_pos  = prompt.index("recent")
        assert rag_pos < summary_pos < recent_pos
```

- [ ] **Step 2: Run tests to confirm FAIL**

```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest services/memory/tests/test_context_manager.py -v 2>&1 | tail -10
```

- [ ] **Step 3: Create `services/memory/context_manager.py`**

```python
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from services.memory.tokenizer import token_count


@dataclass
class ContextBudget:
    max_tokens:         int   = 8192
    completion_reserve: int   = 700
    system_core_share:  float = 0.25
    recent_turns_share: float = 0.30
    rag_share:          float = 0.35
    summary_share:      float = 0.10

    @property
    def effective_budget(self) -> int:
        return self.max_tokens - self.completion_reserve

    def slot(self, share: float) -> int:
        return int(self.effective_budget * share)


@dataclass
class AssembledContext:
    system_prompt:    str
    core_memory:      str
    recent_turns:     str
    retrieved_context: str
    summary_buffer:   str
    total_tokens:     int = 0

    def as_prompt(self) -> str:
        """Assemble with highest-value content near head and recent turns just
        before completion — counters lost-in-the-middle degradation
        (arXiv:2307.03172)."""
        return "\n\n".join(filter(None, [
            self.system_prompt,
            self.core_memory,
            self.retrieved_context,  # reranked evidence near head
            self.summary_buffer,
            self.recent_turns,       # most recent turns just before completion
        ]))


class ContextManager:
    def __init__(
        self,
        redis,
        mongo_db,
        chroma_cols: dict,
        embedder,
        budget: ContextBudget | None = None,
    ) -> None:
        self.redis  = redis
        self.db     = mongo_db
        self.chroma = chroma_cols
        self.embed  = embedder
        self.budget = budget or ContextBudget()

    async def build_context(
        self,
        session_id:    str,
        current_task:  str,
        system_prompt: str,
    ) -> AssembledContext:
        """Assemble the full context for one agent step, strictly within budget."""
        b = self.budget

        # 1. Pinned slots — system prompt + core memory are never trimmed
        core = await self.redis.get(f"core:{session_id}") or ""

        sys_core_tokens = token_count(system_prompt) + token_count(core)
        remaining = b.effective_budget - sys_core_tokens

        # 2. RAG evidence (stub — hybrid_retrieve is Plan B)
        rag_budget = min(int(b.effective_budget * b.rag_share), max(0, remaining))
        rag_text   = ""  # Plan B: await self.hybrid_retrieve(current_task, token_budget=rag_budget)
        remaining -= token_count(rag_text)

        # 3. Summary buffer
        summary_budget = min(int(b.effective_budget * b.summary_share), max(0, remaining))
        summary = await self.redis.get(f"summary:{session_id}") or ""
        summary = self._trim_to_budget(summary, summary_budget)
        remaining -= token_count(summary)

        # 4. Recent turns (newest retained on trim)
        recent_budget = min(int(b.effective_budget * b.recent_turns_share), max(0, remaining))
        recent = await self._recent_turns(session_id, recent_budget)

        ctx = AssembledContext(
            system_prompt=system_prompt,
            core_memory=core,
            recent_turns=recent,
            retrieved_context=rag_text,
            summary_buffer=summary,
        )
        ctx.total_tokens = token_count(ctx.as_prompt())
        return ctx

    def _trim_to_budget(self, text: str, budget: int) -> str:
        """Trim text from the front (oldest lines) until it fits in budget."""
        if token_count(text) <= budget:
            return text
        lines = [l for l in text.splitlines() if l]
        while lines and token_count("\n".join(lines)) > budget:
            lines.pop(0)
        return "\n".join(lines)

    async def _recent_turns(self, session_id: str, budget: int) -> str:
        """Load recent turns from MongoDB, trim to budget (newest retained)."""
        cursor = (
            self.db.messages
            .find({"session_id": session_id}, {"role": 1, "content": 1})
            .sort("seq", -1)
            .limit(50)
        )
        turns = [doc async for doc in cursor]
        turns.reverse()
        lines = [f"{t['role'].upper()}: {t['content']}" for t in turns]
        return self._trim_to_budget("\n".join(lines), budget)
```

- [ ] **Step 4: Run all context manager tests**

```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest services/memory/tests/test_context_manager.py -v
```

Expected: all 5 tests pass.

- [ ] **Step 5: Run full memory test suite**

```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest services/memory/tests/ -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/zachstallbohm/Work/gemma && git add services/memory/context_manager.py services/memory/tests/test_context_manager.py && git commit -m "feat(memory): ContextManager with token budget, build_context, trim, recent_turns"
```

---

## Self-Review

### Spec Coverage

| Spec requirement | Task |
|---|---|
| Motor AsyncIOMotorClient with pool config (maxPoolSize=50, etc.) | Task 3 |
| Chroma AsyncHttpClient (never Persistent/Ephemeral) | Task 3 |
| redis.asyncio ConnectionPool (max_connections=50) | Task 3 |
| MongoDB indexes (messages compound, sessions TTL, tool_calls) | Task 3 |
| `write_message()` single atomic insert with outbox marker | Task 4 |
| `outbox.processed=False` at write time | Task 4 |
| MongoDB `_id` as Chroma point ID (idempotent upsert) | Task 6 |
| Outbox worker tails change stream with resume token | Task 6 |
| Outbox worker marks `processed=True` after Chroma upsert | Task 6 |
| `search_memory()` resolves Chroma hits to MongoDB docs | Task 5 |
| Orphan vector handling (Chroma hit with no Mongo doc) | Task 5 |
| `enqueue_task()` via XADD (not RPUSH/BRPOP) | Task 5 |
| `token_count()` via Gemma AutoTokenizer, lazy singleton | Task 2 |
| `ContextBudget` sub-budget fractions | Task 7 |
| `AssembledContext.as_prompt()` — RAG near head, recent turns near tail | Task 7 |
| `build_context()` — pinned core memory never trimmed | Task 7 |
| `_trim_to_budget()` — drops oldest lines first | Task 7 |
| `_recent_turns()` — chronological, newest retained on trim | Task 7 |

**Deferred to Plan B (noted, not missing):**
- `hybrid_retrieve()` — stubbed as empty string in `build_context()`
- Consolidation worker — requires LLM callbacks
- FlagReranker + BM25 — require GPU models
- `token_count()` assertion in `build_context()` — removed (pinned core can exceed rag slot, and that's correct behavior; only outright window overflow is an error)

### Placeholder Scan

None — all stubs either raise `NotImplementedError` (replaced in later tasks) or return empty string with a `# Plan B` comment.

### Type Consistency

- `StorageManager.write_message()` returns `ObjectId` — used as such in tests ✓
- `ContextManager._trim_to_budget()` defined in Task 7, called in `build_context()` and `_recent_turns()` in same task ✓
- `token_count` imported from `services.memory.tokenizer` in both `context_manager.py` and patched by the same path in tests ✓
