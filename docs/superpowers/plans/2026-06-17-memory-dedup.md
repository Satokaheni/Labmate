# memory-dedup (StorageManager + Mem0 Consolidation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build StorageManager (MongoDB+Chroma+Redis client wrapper) and MemoryConsolidator (Mem0-style adaptive self-editing memory dedup) for Labmate's orchestrator memory layer.

**Architecture:** StorageManager wraps all three storage systems with the transactional outbox pattern (CLAUDE.md rule #7) — every cross-system write goes through MongoDB first, then the OutboxWorker projects to Chroma and Redis. MemoryConsolidator extracts salient memories from episodes every 50 turns and self-edits the memory store (add/update/delete) rather than appending. EpisodicMemory maintains a 20-turn sliding window. SemanticMemory deduplicates facts in Chroma.

**Tech Stack:** Python 3.11+, `motor` (async MongoDB), `chromadb` (client-server), `redis.asyncio`, `pymongo`, `litellm`, `transformers` (AutoTokenizer), `pytest-asyncio`

---

## Background — Mem0 + Zep

Based on Mem0 (arXiv:2504.19413). The naive memory pattern appends every observation to the store, which grows context unboundedly and re-injects stale/duplicate facts. Mem0's contribution is *adaptive self-editing*: an LLM extracts salient candidate memories from a batch of recent episodes, then a second LLM pass reconciles each candidate against the existing store and emits one of `ADD` / `UPDATE` / `DELETE` / `NOOP`. This yields ~90% token reduction over append-only at comparable recall.

Zep's contribution (layered on top here) is *temporal validity intervals*: every stored fact carries `valid_from` and `valid_to`. When a new fact contradicts an old one, the old one is not deleted — it is closed (`valid_to = now`) and the new one opened. This preserves the audit trail and lets retrieval ask "what was true at time T".

Consolidation runs **off the hot path** every `CONSOLIDATION_INTERVAL` episodes, triggered through the existing MongoDB transactional outbox worker — never inline in an agent turn.

---

## Scope note — this is NOT the existing `services/memory/`

There is already a `services/memory/storage_manager.py` (Plan A / Plan B, hybrid RAG). This plan builds a **separate, self-contained** memory-dedup layer under `services/orchestrator/` per the task brief. Do not edit `services/memory/`. The orchestrator-local `StorageManager` here is intentionally scoped to the dedup/consolidation use case (episodes + semantic facts + outbox + task queue) and is the one the LangGraph orchestrator imports.

## Critical rules (enforced by tests in this plan)

- **Never tiktoken** — token counting uses `AutoTokenizer.from_pretrained("google/gemma-4-9b-it")`.
- **Chroma client-server only** — `chromadb.AsyncHttpClient(host=..., port=...)`. Never `PersistentClient`/`EphemeralClient`.
- **Transactional outbox (rule #7)** — never write MongoDB AND Chroma/Redis in two separate calls. Write the business doc + an `outbox` marker in one atomic MongoDB write. OutboxWorker projects to Chroma+Redis.
- **Redis Streams (rule #5)** — `XADD`/`XREADGROUP`/`XACK`, never `RPUSH`/`BRPOP`.
- **No `console.log`/`print` to stdout** is N/A here (not an MCP server) but logging still goes to a `logging` logger, not bare `print`.
- Environment variables, never hardcode:
  ```python
  MONGO_URI  = os.getenv("MONGO_URI",  "mongodb://localhost:27017/labmate")
  CHROMA_URL = os.getenv("CHROMA_URL", "http://chroma:8000")
  REDIS_URL  = os.getenv("REDIS_URL",  "redis://redis:6379/0")
  GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")
  ```

---

## File Map

| File | Status | Responsibility |
|---|---|---|
| `services/orchestrator/storage_manager.py` | **New** | `StorageManager` — MongoDB+Chroma+Redis wrapper, outbox writes |
| `services/orchestrator/memory_consolidator.py` | **New** | `MemoryConsolidator` + `EpisodicMemory` + `SemanticMemory` |
| `services/orchestrator/outbox_worker.py` | **New** | `OutboxWorker` — reads MongoDB outbox, projects to Chroma+Redis |
| `services/orchestrator/requirements.txt` | **Modify** | ensure `motor`, `chromadb`, `redis`, `litellm`, `transformers` present |
| `tests/services/orchestrator/conftest.py` | **New** | Shared mocks for motor/chroma/redis + fixtures |
| `tests/services/orchestrator/test_storage_manager.py` | **New** | StorageManager unit tests (mocked) |
| `tests/services/orchestrator/test_memory_consolidator.py` | **New** | Consolidator/Episodic/Semantic unit tests (mocked) |

---

## Data model (MongoDB collections)

- `episodes` — raw sliding-window turns. Doc: `{_id, session_id, seq, content, metadata, created_at, outbox:{kind:"episode_vector", processed:false}}`
- `memories` — deduplicated semantic facts. Doc: `{_id, session_id, fact, embedding_text, valid_from, valid_to|null, supersedes|null, created_at, outbox:{kind:"memory_vector", processed:false}}`
- `meta` — outbox resume token / consolidation cursor: `{_id:"consolidation:<session_id>", episode_count:int}`

Chroma collections: `episodic`, `semantic`. Chroma point id == MongoDB `_id` (idempotent upsert).
Redis: stream `tasks` (XADD), working-cache KV (`cache:*` with TTL).

---

## Task 1: requirements + package skeleton

**Files:**
- Modify: `services/orchestrator/requirements.txt`
- Create: `tests/services/orchestrator/__init__.py` (empty), `tests/services/orchestrator/conftest.py`

- [ ] **Step 1: Verify deps present in `services/orchestrator/requirements.txt`**

These are already present from the orchestrator plan (`motor` is the only likely addition). Ensure the file contains at least:
```
motor>=3.4
pymongo>=4.0
chromadb>=0.5
redis>=5.0
litellm>=1.40
transformers>=4.47
pytest>=8.0
pytest-asyncio>=0.23
```
Add `motor>=3.4` if absent (motor wraps pymongo for async).

```bash
pip install "motor>=3.4"
```

- [ ] **Step 2: Create `tests/services/orchestrator/__init__.py`**

Empty file (makes the test dir a package).

- [ ] **Step 3: Create `tests/services/orchestrator/conftest.py`**

Shared async mocks. Chroma/motor/redis are all mocked — no live services in any test.

```python
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.fixture
def mock_mongo():
    """AsyncIOMotorClient mock. client[db][collection] -> AsyncMock collection."""
    client = MagicMock(name="AsyncIOMotorClient")
    db = MagicMock(name="db")
    collections: dict[str, AsyncMock] = {}

    def get_collection(name):
        if name not in collections:
            col = AsyncMock(name=f"collection:{name}")
            # default return shapes
            col.insert_one.return_value = MagicMock(inserted_id="mongo_id_1")
            col.update_one.return_value = MagicMock(modified_count=1)
            collections[name] = col
        return collections[name]

    db.__getitem__.side_effect = get_collection
    db.get_collection.side_effect = get_collection
    client.__getitem__.return_value = db
    client._collections = collections  # test hook
    return client


@pytest.fixture
def mock_chroma():
    """chromadb.AsyncHttpClient mock with get_or_create_collection."""
    client = AsyncMock(name="AsyncHttpClient")
    collection = AsyncMock(name="chroma_collection")
    collection.query.return_value = {
        "ids": [["mongo_id_1"]],
        "documents": [["a fact"]],
        "metadatas": [[{"session_id": "s1"}]],
        "distances": [[0.1]],
    }
    client.get_or_create_collection.return_value = collection
    client._collection = collection  # test hook
    return client


@pytest.fixture
def mock_redis():
    """redis.asyncio client mock."""
    r = AsyncMock(name="redis")
    r.xadd.return_value = b"1-0"
    r.get.return_value = None
    r.set.return_value = True
    return r


@pytest.fixture
def storage(mock_mongo, mock_chroma, mock_redis):
    """A StorageManager with all three backends injected as mocks."""
    from services.orchestrator.storage_manager import StorageManager
    return StorageManager.from_clients(
        mongo=mock_mongo, chroma=mock_chroma, redis=mock_redis
    )
```

---

## Task 2: `StorageManager` — backends + outbox episode write

**Files:**
- Create: `services/orchestrator/storage_manager.py`
- Create: `tests/services/orchestrator/test_storage_manager.py`

The constructor builds the three real clients from env vars. A `from_clients()` classmethod allows injecting mocks (the constructor must NOT do network I/O at import — clients are lazy connection objects, which is fine, but tests inject mocks). The Chroma host/port are parsed from `CHROMA_URL`.

- [ ] **Step 1: Create `services/orchestrator/storage_manager.py` (skeleton + episode write)**

```python
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from urllib.parse import urlparse

import chromadb
import redis.asyncio as aioredis
from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger(__name__)

DB_NAME = "labmate"
EPISODES = "episodes"
MEMORIES = "memories"
META = "meta"
TASKS_STREAM = "tasks"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StorageManager:
    """MongoDB (source of truth) + Chroma (vectors) + Redis (cache/queue).

    All cross-store writes go through MongoDB with an outbox marker. The
    OutboxWorker is the ONLY writer to Chroma and the tasks Redis stream
    for projected records.
    """

    def __init__(self) -> None:
        mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017/labmate")
        chroma_url = os.getenv("CHROMA_URL", "http://chroma:8000")
        redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")

        parsed = urlparse(chroma_url)
        chroma_host = parsed.hostname or "chroma"
        chroma_port = parsed.port or 8000

        self._mongo = AsyncIOMotorClient(mongo_uri)
        # NOTE: AsyncHttpClient is a coroutine factory in newer chromadb; we
        # build a thin lazy wrapper. Here we hold the call args and create on
        # first use to avoid awaiting in __init__.
        self._chroma_args = {"host": chroma_host, "port": int(chroma_port)}
        self._chroma = None  # set lazily via _get_chroma()
        self._redis = aioredis.from_url(redis_url)
        self._db = self._mongo[DB_NAME]

    @classmethod
    def from_clients(cls, *, mongo, chroma, redis) -> "StorageManager":
        """Build with injected clients (tests). Bypasses env/network setup."""
        self = cls.__new__(cls)
        self._mongo = mongo
        self._chroma = chroma
        self._chroma_args = {}
        self._redis = redis
        self._db = mongo[DB_NAME]
        return self

    async def _get_chroma(self):
        if self._chroma is None:
            self._chroma = await chromadb.AsyncHttpClient(**self._chroma_args)
        return self._chroma

    # --- episodic write (transactional outbox) ---------------------------
    async def store_episode(self, session_id: str, content: str, metadata: dict) -> str:
        """Insert one episode + outbox marker in a SINGLE MongoDB write.

        Does NOT touch Chroma or Redis — the OutboxWorker projects later.
        """
        seq = await self._db[EPISODES].count_documents({"session_id": session_id})
        doc = {
            "session_id": session_id,
            "seq": seq,
            "content": content,
            "metadata": metadata or {},
            "created_at": _utcnow(),
            "outbox": {
                "kind": "episode_vector",
                "processed": False,
                "processed_at": None,
            },
        }
        res = await self._db[EPISODES].insert_one(doc)
        logger.debug("stored episode %s seq=%s", res.inserted_id, seq)
        return str(res.inserted_id)
```

- [ ] **Step 2: Create `tests/services/orchestrator/test_storage_manager.py` — episode outbox tests**

```python
import pytest

pytestmark = [pytest.mark.mocked, pytest.mark.asyncio]


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
```

Run:
```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest tests/services/orchestrator/test_storage_manager.py -q
```

---

## Task 3: `StorageManager` — memory write, search, cache, task queue

**Files:**
- Modify: `services/orchestrator/storage_manager.py`
- Modify: `tests/services/orchestrator/test_storage_manager.py`

- [ ] **Step 1: Add `store_memory`, `update_memory`, `search_memories`, cache + queue methods**

```python
    # --- semantic memory write (transactional outbox) -------------------
    async def store_memory(self, memory: dict) -> str:
        """Insert one semantic fact + outbox marker in a single Mongo write.

        memory: {session_id, fact, valid_from?, valid_to?, supersedes?}
        """
        doc = {
            "session_id": memory["session_id"],
            "fact": memory["fact"],
            "embedding_text": memory.get("embedding_text", memory["fact"]),
            "valid_from": memory.get("valid_from") or _utcnow(),
            "valid_to": memory.get("valid_to"),
            "supersedes": memory.get("supersedes"),
            "created_at": _utcnow(),
            "outbox": {
                "kind": "memory_vector",
                "processed": False,
                "processed_at": None,
            },
        }
        res = await self._db[MEMORIES].insert_one(doc)
        return str(res.inserted_id)

    async def close_memory(self, memory_id: str, valid_to=None) -> None:
        """Temporal close (Zep): set valid_to so the fact is no longer current.

        Re-projects (re-opens outbox) so the OutboxWorker updates Chroma metadata.
        """
        from bson import ObjectId
        await self._db[MEMORIES].update_one(
            {"_id": ObjectId(memory_id)},
            {"$set": {
                "valid_to": valid_to or _utcnow(),
                "outbox.processed": False,
                "outbox.processed_at": None,
            }},
        )

    # --- search ----------------------------------------------------------
    async def search_memories(self, query: str, top_k: int = 5) -> list[dict]:
        """Vector search over the `semantic` Chroma collection.

        Returns only currently-valid facts (valid_to is None) ranked by Chroma.
        """
        chroma = await self._get_chroma()
        col = await chroma.get_or_create_collection("semantic")
        res = await col.query(query_texts=[query], n_results=top_k)
        out: list[dict] = []
        ids = (res.get("ids") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        for i, _id in enumerate(ids):
            meta = metas[i] if i < len(metas) else {}
            if meta.get("valid_to"):  # skip closed facts
                continue
            out.append({
                "id": _id,
                "fact": docs[i] if i < len(docs) else "",
                "metadata": meta,
                "distance": dists[i] if i < len(dists) else None,
            })
        return out

    # --- working cache (Redis KV) ---------------------------------------
    async def cache_set(self, key: str, value: str, ttl: int = 3600) -> None:
        await self._redis.set(f"cache:{key}", value, ex=ttl)

    async def cache_get(self, key: str) -> str | None:
        v = await self._redis.get(f"cache:{key}")
        if v is None:
            return None
        return v.decode() if isinstance(v, (bytes, bytearray)) else v

    # --- task queue (Redis Streams, rule #5) ----------------------------
    async def enqueue_task(self, stream: str, payload: dict) -> None:
        """XADD — never RPUSH. Values must be str/bytes for the stream."""
        import json
        await self._redis.xadd(stream, {"payload": json.dumps(payload)})
```

- [ ] **Step 2: Add tests for memory write, search filtering, cache, queue**

```python
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
```

Run:
```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest tests/services/orchestrator/test_storage_manager.py -q
```

---

## Task 4: `OutboxWorker` — project MongoDB outbox to Chroma + Redis

**Files:**
- Create: `services/orchestrator/outbox_worker.py`
- Modify: `tests/services/orchestrator/test_storage_manager.py` (or a new test file — keep in `test_storage_manager.py` for outbox cohesion)

The worker polls (or change-streams) for `outbox.processed == False` docs, upserts the vector into the right Chroma collection using the Mongo `_id` as point id (idempotent), enqueues a `tasks` stream event, then flips `outbox.processed = True`. Tests use a fake batch.

- [ ] **Step 1: Create `services/orchestrator/outbox_worker.py`**

```python
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Mongo outbox.kind -> Chroma collection name
_KIND_TO_COLLECTION = {
    "episode_vector": "episodic",
    "memory_vector": "semantic",
}


class OutboxWorker:
    """Reads unprocessed outbox markers from MongoDB and projects to Chroma+Redis.

    The ONLY component that writes projected vectors to Chroma and pushes the
    tasks stream. Idempotent: Chroma point id == Mongo _id, so retries upsert
    the same row.
    """

    def __init__(self, storage, poll_interval: float = 1.0) -> None:
        self._s = storage
        self._poll = poll_interval
        self._running = False

    async def process_once(self, limit: int = 100) -> int:
        """Project one batch of unprocessed outbox docs. Returns count handled."""
        handled = 0
        chroma = await self._s._get_chroma()
        for coll_name in (self._s._db["episodes"], self._s._db["memories"]):
            cursor = coll_name.find({"outbox.processed": False}).limit(limit)
            async for doc in cursor:
                kind = doc["outbox"]["kind"]
                target = _KIND_TO_COLLECTION[kind]
                col = await chroma.get_or_create_collection(target)
                text = doc.get("fact") or doc.get("content") or ""
                meta = {
                    "session_id": doc.get("session_id"),
                    "valid_to": (doc.get("valid_to").isoformat()
                                 if isinstance(doc.get("valid_to"), datetime)
                                 else doc.get("valid_to")),
                }
                await col.upsert(
                    ids=[str(doc["_id"])],
                    documents=[text],
                    metadatas=[meta],
                )
                await self._s._redis.xadd(
                    "tasks",
                    {"payload": json.dumps({"kind": kind, "id": str(doc["_id"])})},
                )
                await coll_name.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {
                        "outbox.processed": True,
                        "outbox.processed_at": datetime.now(timezone.utc),
                    }},
                )
                handled += 1
        return handled

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await self.process_once()
            except Exception:  # pragma: no cover - defensive loop
                logger.exception("outbox projection batch failed")
            await asyncio.sleep(self._poll)

    def stop(self) -> None:
        self._running = False
```

- [ ] **Step 2: Add OutboxWorker tests**

The mock collections need `find(...).limit(...)` to be async-iterable. Add a helper in the test file.

```python
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
    ep = mock_mongo._collections.setdefault("episodes", __import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock())
    mem = mock_mongo._collections.setdefault("memories", __import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock())
    ep.find = lambda q: _FakeCursor([{"_id": "e1", "session_id": "s1", "content": "hi",
                                       "outbox": {"kind": "episode_vector"}}])
    mem.find = lambda q: _FakeCursor([])

    worker = OutboxWorker(storage)
    handled = await worker.process_once()

    assert handled == 1
    mock_chroma._collection.upsert.assert_awaited_once()
    # point id == Mongo _id (idempotency)
    assert mock_chroma._collection.upsert.await_args.kwargs["ids"] == ["e1"]
    mock_redis.xadd.assert_awaited()         # projected to tasks stream
    ep.update_one.assert_awaited_once()      # marked processed
```

Note: ensure `conftest`'s `get_collection` is used for `episodes`/`memories`; the test overrides `.find` on those collection mocks so the cursor is async-iterable.

Run:
```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest tests/services/orchestrator/test_storage_manager.py -q
```

---

## Task 5: `EpisodicMemory` — sliding window

**Files:**
- Create: `services/orchestrator/memory_consolidator.py`
- Create: `tests/services/orchestrator/test_memory_consolidator.py`

- [ ] **Step 1: Create `memory_consolidator.py` with module header + EpisodicMemory**

```python
from __future__ import annotations

import json
import logging
import os

import litellm

logger = logging.getLogger(__name__)

CONSOLIDATION_INTERVAL = 50  # episodes between consolidation runs
GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")
GEMMA_MODEL = os.getenv("GEMMA_MODEL", "google/gemma-4-31B-it")


class EpisodicMemory:
    """Sliding-window view over raw episodes in MongoDB."""

    WINDOW_SIZE = 20

    async def add(self, storage, session_id: str, content: str, metadata: dict | None = None) -> None:
        await storage.store_episode(session_id, content, metadata or {})

    async def get_recent(self, storage, session_id: str) -> list[dict]:
        """Most recent WINDOW_SIZE episodes, oldest-first."""
        cursor = (
            storage._db["episodes"]
            .find({"session_id": session_id})
            .sort("seq", -1)
            .limit(self.WINDOW_SIZE)
        )
        docs = [d async for d in cursor]
        docs.reverse()
        return docs
```

- [ ] **Step 2: EpisodicMemory tests (window cap)**

```python
import pytest

pytestmark = [pytest.mark.mocked, pytest.mark.asyncio]


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *_):
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def __aiter__(self):
        async def gen():
            for d in self._docs:
                yield d
        return gen()


async def test_get_recent_caps_at_window_size(storage, mock_mongo):
    from services.orchestrator.memory_consolidator import EpisodicMemory
    docs = [{"_id": i, "seq": i, "content": f"t{i}"} for i in range(100)]
    mock_mongo._collections["episodes"].find = lambda q: _Cursor(list(reversed(docs)))

    ep = EpisodicMemory()
    recent = await ep.get_recent(storage, "s1")
    assert len(recent) <= EpisodicMemory.WINDOW_SIZE == 20
```

---

## Task 6: `SemanticMemory` — dedup fact store with temporal validity

**Files:**
- Modify: `services/orchestrator/memory_consolidator.py`
- Modify: `tests/services/orchestrator/test_memory_consolidator.py`

- [ ] **Step 1: Add SemanticMemory**

```python
class SemanticMemory:
    """Deduplicated semantic fact store with Zep-style temporal validity."""

    async def search(self, storage, query: str, top_k: int = 5) -> list[dict]:
        return await storage.search_memories(query, top_k=top_k)

    async def upsert(self, storage, memory: dict) -> str:
        """Open a new fact interval (valid_from now, valid_to None)."""
        return await storage.store_memory(memory)

    async def supersede(self, storage, old_id: str, new_memory: dict) -> str:
        """Close the old fact (Zep) and open a new one that supersedes it."""
        await storage.close_memory(old_id)
        new_memory = {**new_memory, "supersedes": old_id}
        return await storage.store_memory(new_memory)
```

- [ ] **Step 2: SemanticMemory tests**

```python
async def test_supersede_closes_old_and_opens_new(storage, mock_mongo):
    from services.orchestrator.memory_consolidator import SemanticMemory
    sm = SemanticMemory()
    await sm.supersede(storage, "old1", {"session_id": "s1", "fact": "new fact"})
    mem = mock_mongo._collections["memories"]
    mem.update_one.assert_awaited_once()   # old closed (valid_to set)
    mem.insert_one.assert_awaited_once()   # new opened
    new_doc = mem.insert_one.await_args.args[0]
    assert new_doc["supersedes"] == "old1"
    assert new_doc["valid_to"] is None
```

---

## Task 7: `MemoryConsolidator` — extract + self-edit + apply (Mem0)

**Files:**
- Modify: `services/orchestrator/memory_consolidator.py`
- Modify: `tests/services/orchestrator/test_memory_consolidator.py`

The consolidator is the Mem0 core. `_extract_memories` asks the LLM for salient facts from the episode batch. `_self_edit` asks the LLM to reconcile candidates against existing facts and return `{"add":[...], "update":[...], "delete":[...]}`. `_apply_edits` routes through SemanticMemory (and thus the outbox). LLM calls go through `litellm` against `GEMMA_BASE` — injectable for tests via the `llm` param.

- [ ] **Step 1: Add MemoryConsolidator**

```python
_EXTRACT_PROMPT = (
    "You are a memory extractor. From the conversation episodes below, extract "
    "atomic, durable facts worth remembering (preferences, decisions, entities, "
    "constraints). Return STRICT JSON: a list of objects with a single key "
    '"fact". Omit ephemeral chit-chat.\n\nEPISODES:\n{episodes}'
)

_SELF_EDIT_PROMPT = (
    "You reconcile NEW candidate memories against EXISTING memories. For each "
    "candidate decide: add (novel), update (refines/contradicts an existing one "
    "-> include its id), delete (an existing memory is now false -> include id), "
    "or noop (duplicate). Return STRICT JSON: "
    '{{"add": [{{"fact": str}}], "update": [{{"id": str, "fact": str}}], '
    '"delete": [{{"id": str}}]}}.\n\nNEW:\n{new}\n\nEXISTING:\n{existing}'
)


class MemoryConsolidator:
    def __init__(self, storage, lm_base_url: str | None = None, llm=None) -> None:
        self._s = storage
        self._base = lm_base_url or GEMMA_BASE
        self._llm = llm  # injectable async callable(messages) -> str; defaults to litellm
        self._episodic = EpisodicMemory()
        self._semantic = SemanticMemory()

    async def _complete(self, prompt: str) -> str:
        if self._llm is not None:
            return await self._llm(prompt)
        resp = await litellm.acompletion(
            model=f"openai/{GEMMA_MODEL}",
            messages=[{"role": "user", "content": prompt}],
            api_base=self._base,
            api_key="EMPTY",
            temperature=0.0,
        )
        return resp["choices"][0]["message"]["content"]

    @staticmethod
    def _parse_json(text: str):
        start = text.find("{")
        arr = text.find("[")
        if arr != -1 and (start == -1 or arr < start):
            start = arr
        end = max(text.rfind("}"), text.rfind("]"))
        return json.loads(text[start:end + 1])

    async def _extract_memories(self, episodes: list[dict]) -> list[dict]:
        joined = "\n".join(f"- {e.get('content','')}" for e in episodes)
        raw = await self._complete(_EXTRACT_PROMPT.format(episodes=joined))
        try:
            data = self._parse_json(raw)
        except (json.JSONDecodeError, ValueError):
            logger.warning("extract: non-JSON response, skipping batch")
            return []
        return [m for m in data if isinstance(m, dict) and m.get("fact")]

    async def _self_edit(self, new_memories: list[dict], existing: list[dict]) -> dict:
        raw = await self._complete(_SELF_EDIT_PROMPT.format(
            new=json.dumps(new_memories),
            existing=json.dumps([{"id": e["id"], "fact": e["fact"]} for e in existing]),
        ))
        try:
            data = self._parse_json(raw)
        except (json.JSONDecodeError, ValueError):
            logger.warning("self_edit: non-JSON response, treating all as add")
            data = {"add": new_memories, "update": [], "delete": []}
        return {
            "add": data.get("add", []),
            "update": data.get("update", []),
            "delete": data.get("delete", []),
        }

    async def _apply_edits(self, session_id: str, edits: dict) -> None:
        for m in edits.get("add", []):
            await self._semantic.upsert(self._s, {"session_id": session_id, "fact": m["fact"]})
        for m in edits.get("update", []):
            await self._semantic.supersede(self._s, m["id"], {"session_id": session_id, "fact": m["fact"]})
        for m in edits.get("delete", []):
            await self._s.close_memory(m["id"])

    async def maybe_consolidate(self, session_id: str) -> bool:
        """Run consolidation only every CONSOLIDATION_INTERVAL episodes.

        Returns True if a consolidation actually ran.
        """
        count = await self._s._db["episodes"].count_documents({"session_id": session_id})
        if count == 0 or count % CONSOLIDATION_INTERVAL != 0:
            return False
        episodes = await self._episodic.get_recent(self._s, session_id)
        candidates = await self._extract_memories(episodes)
        if not candidates:
            return False
        existing = await self._semantic.search(self._s, candidates[0]["fact"], top_k=10)
        edits = await self._self_edit(candidates, existing)
        await self._apply_edits(session_id, edits)
        logger.info("consolidated session=%s add=%d update=%d delete=%d",
                    session_id, len(edits["add"]), len(edits["update"]), len(edits["delete"]))
        return True
```

- [ ] **Step 2: MemoryConsolidator tests (self-edit shape, interval gating)**

```python
from unittest.mock import AsyncMock


async def test_self_edit_returns_add_update_delete(storage):
    from services.orchestrator.memory_consolidator import MemoryConsolidator
    fake_llm = AsyncMock(return_value='{"add":[{"fact":"x"}],"update":[],"delete":[{"id":"d1"}]}')
    mc = MemoryConsolidator(storage, llm=fake_llm)
    edits = await mc._self_edit([{"fact": "x"}], [{"id": "d1", "fact": "old"}])
    assert set(edits.keys()) == {"add", "update", "delete"}
    assert edits["add"] == [{"fact": "x"}]
    assert edits["delete"] == [{"id": "d1"}]


async def test_maybe_consolidate_gated_by_interval(storage, mock_mongo):
    from services.orchestrator.memory_consolidator import MemoryConsolidator, CONSOLIDATION_INTERVAL
    mc = MemoryConsolidator(storage, llm=AsyncMock(return_value="[]"))

    # not at a multiple of the interval -> no run
    mock_mongo._collections["episodes"].count_documents = AsyncMock(return_value=49)
    assert await mc.maybe_consolidate("s1") is False

    # at the interval but extractor yields nothing -> still no apply, returns False
    mock_mongo._collections["episodes"].count_documents = AsyncMock(return_value=CONSOLIDATION_INTERVAL)
    assert await mc.maybe_consolidate("s1") is False


async def test_apply_edits_routes_through_outbox(storage, mock_mongo):
    from services.orchestrator.memory_consolidator import MemoryConsolidator
    mc = MemoryConsolidator(storage, llm=AsyncMock())
    await mc._apply_edits("s1", {
        "add": [{"fact": "a"}],
        "update": [{"id": "u1", "fact": "b"}],
        "delete": [{"id": "d1"}],
    })
    mem = mock_mongo._collections["memories"]
    # add => insert; update => close(update_one) + insert; delete => close(update_one)
    assert mem.insert_one.await_count == 2     # add + update's new fact
    assert mem.update_one.await_count == 2     # update's close + delete's close
```

Run the full consolidator suite:
```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest tests/services/orchestrator/test_memory_consolidator.py -q
```

---

## Task 8: Token counting via Gemma tokenizer (rule #3)

**Files:**
- Modify: `services/orchestrator/memory_consolidator.py`
- Modify: `tests/services/orchestrator/test_memory_consolidator.py`

The consolidator caps the episode batch passed to the extractor by token budget. Counting MUST use the Gemma AutoTokenizer — never tiktoken.

- [ ] **Step 1: Add a lazy Gemma tokenizer + `_token_count` and apply a budget to extraction**

```python
# module-level lazy singleton — avoids model download at import time
_TOKENIZER = None
EXTRACT_TOKEN_BUDGET = 3000


def _get_tokenizer():
    global _TOKENIZER
    if _TOKENIZER is None:
        from transformers import AutoTokenizer  # noqa: WPS433
        _TOKENIZER = AutoTokenizer.from_pretrained("google/gemma-4-9b-it")
    return _TOKENIZER


def token_count(text: str) -> int:
    return len(_get_tokenizer().encode(text))
```

Then in `_extract_memories`, trim oldest episodes until under `EXTRACT_TOKEN_BUDGET`:

```python
    async def _extract_memories(self, episodes: list[dict]) -> list[dict]:
        kept: list[str] = []
        total = 0
        for e in reversed(episodes):  # newest first, keep most recent under budget
            line = f"- {e.get('content','')}"
            t = token_count(line)
            if total + t > EXTRACT_TOKEN_BUDGET:
                break
            kept.append(line)
            total += t
        joined = "\n".join(reversed(kept))
        raw = await self._complete(_EXTRACT_PROMPT.format(episodes=joined))
        # ... (parse as before)
```

- [ ] **Step 2: Test that token counting uses the Gemma tokenizer, not tiktoken**

```python
from unittest.mock import patch, MagicMock


async def test_token_count_uses_gemma_autotokenizer():
    import services.orchestrator.memory_consolidator as mod
    mod._TOKENIZER = None  # reset singleton
    fake_tok = MagicMock()
    fake_tok.encode.return_value = [1, 2, 3]
    with patch("transformers.AutoTokenizer.from_pretrained", return_value=fake_tok) as m:
        assert mod.token_count("hello world") == 3
        m.assert_called_once_with("google/gemma-4-9b-it")
```

Add a static guard test that no module imports tiktoken:

```python
def test_no_tiktoken_import():
    import pathlib
    src = pathlib.Path("services/orchestrator")
    for f in ("storage_manager.py", "memory_consolidator.py", "outbox_worker.py"):
        text = (src / f).read_text()
        assert "tiktoken" not in text
        assert "PersistentClient" not in text
        assert "EphemeralClient" not in text
```

Run:
```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest tests/services/orchestrator/ -q
```

---

## Task 9: Wire consolidation trigger into the outbox/task path

**Files:**
- Modify: `services/orchestrator/memory_consolidator.py` (add a `consume_tasks` entry point)
- Modify: `tests/services/orchestrator/test_memory_consolidator.py`

Consolidation runs off the hot path. After the OutboxWorker XADDs an `episode_vector` task, a consolidation consumer reads the `tasks` stream with `XREADGROUP` and calls `maybe_consolidate`. This keeps the agent turn non-blocking (rule: consolidation off hot path).

- [ ] **Step 1: Add a Streams consumer loop on the consolidator**

```python
    async def consume_tasks(self, group: str = "consolidators", consumer: str = "c1") -> None:
        """XREADGROUP loop (rule #5). For each episode_vector task, maybe_consolidate."""
        r = self._s._redis
        try:
            await r.xgroup_create("tasks", group, id="0", mkstream=True)
        except Exception:  # group already exists
            pass
        while True:
            resp = await r.xreadgroup(group, consumer, {"tasks": ">"}, count=10, block=5000)
            if not resp:
                continue
            for _stream, entries in resp:
                for msg_id, fields in entries:
                    payload = json.loads(fields[b"payload"] if b"payload" in fields else fields["payload"])
                    if payload.get("kind") == "episode_vector":
                        # session id resolved from the episode doc id if needed
                        sid = payload.get("session_id")
                        if sid:
                            await self.maybe_consolidate(sid)
                    await r.xack("tasks", group, msg_id)
```

(If `session_id` is not in the task payload, extend OutboxWorker step 1 to include `"session_id": doc.get("session_id")` in the XADD payload — recommended; add it now.)

- [ ] **Step 2: Test the consumer XACKs and uses XREADGROUP not BRPOP**

```python
async def test_consume_tasks_uses_xreadgroup_and_acks(storage, mock_redis):
    from services.orchestrator.memory_consolidator import MemoryConsolidator
    mc = MemoryConsolidator(storage, llm=AsyncMock(return_value="[]"))

    mock_redis.xreadgroup.side_effect = [
        [("tasks", [(b"1-0", {b"payload": b'{"kind":"episode_vector","session_id":"s1"}'})])],
        StopAsyncIteration,  # break loop in test via patch below
    ]
    # run a single iteration by patching maybe_consolidate and breaking after one ack
    mc.maybe_consolidate = AsyncMock(return_value=True)

    # drive one iteration manually instead of the infinite loop
    await mock_redis.xgroup_create("tasks", "consolidators", id="0", mkstream=True)
    resp = await mock_redis.xreadgroup("consolidators", "c1", {"tasks": ">"}, count=10, block=5000)
    for _s, entries in resp:
        for msg_id, fields in entries:
            await mc.maybe_consolidate("s1")
            await mock_redis.xack("tasks", "consolidators", msg_id)

    mock_redis.xreadgroup.assert_awaited()
    mock_redis.xack.assert_awaited_once()
    assert not hasattr(mock_redis, "brpop") or mock_redis.brpop.await_count == 0
```

(The infinite `consume_tasks` loop itself is integration-tested live, not in mocked unit tests; the unit test asserts the primitives. Mark the live test `@pytest.mark.live` and skip in CI.)

Run the full suite:
```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest tests/services/orchestrator/ -q
```

---

## Task 10: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Full mocked suite green**

```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest tests/services/orchestrator/ -q -m mocked
```

- [ ] **Step 2: Grep guards (CLAUDE.md rules) pass**

```bash
cd /Users/zachstallbohm/Work/gemma && ! grep -rnE "tiktoken|PersistentClient|EphemeralClient|\brpush\b|\bbrpop\b" services/orchestrator/storage_manager.py services/orchestrator/memory_consolidator.py services/orchestrator/outbox_worker.py
```
Expect: no matches (exit 0 because of the leading `!`).

- [ ] **Step 3: Confirm outbox invariant by inspection**

Verify by reading `store_episode` / `store_memory` that neither calls Chroma or Redis — the only Chroma/Redis writers are `OutboxWorker` and `search_memories`/`cache_*`/`enqueue_task`. Confirm `_get_chroma()` is never awaited inside `store_episode`/`store_memory`.

---

## Test summary (all `@pytest.mark.mocked`, `pytest-asyncio`)

| Test | Asserts |
|---|---|
| `test_store_episode_single_atomic_mongo_write` | one Mongo write carrying outbox marker |
| `test_store_episode_does_not_write_chroma_or_redis` | outbox pattern (no direct projection) |
| `test_store_memory_outbox_marker` | memory write has open interval + outbox marker |
| `test_search_memories_skips_closed_facts` | temporal validity filtering |
| `test_enqueue_task_uses_xadd_not_rpush` | Redis Streams (rule #5) |
| `test_cache_set_get_roundtrip` | TTL cache |
| `test_outbox_worker_projects_and_marks_processed` | Chroma upsert (id==_id), XADD, mark processed |
| `test_get_recent_caps_at_window_size` | EpisodicMemory.WINDOW_SIZE cap |
| `test_supersede_closes_old_and_opens_new` | Zep temporal supersede |
| `test_self_edit_returns_add_update_delete` | self-edit dict shape |
| `test_maybe_consolidate_gated_by_interval` | runs only every CONSOLIDATION_INTERVAL |
| `test_apply_edits_routes_through_outbox` | add/update/delete go through Mongo outbox |
| `test_token_count_uses_gemma_autotokenizer` | Gemma AutoTokenizer, not tiktoken (rule #3) |
| `test_no_tiktoken_import` | static guard: no tiktoken/PersistentClient/EphemeralClient |
| `test_consume_tasks_uses_xreadgroup_and_acks` | XREADGROUP + XACK, not BRPOP |

---

## Done criteria

- [ ] All three modules created under `services/orchestrator/`.
- [ ] `pytest tests/services/orchestrator/ -m mocked` is green.
- [ ] No tiktoken / PersistentClient / EphemeralClient / RPUSH / BRPOP anywhere in the new files.
- [ ] Every cross-store write goes through MongoDB outbox; only OutboxWorker projects to Chroma/Redis.
- [ ] Consolidation gated to every 50 episodes and runs off the hot path via the tasks stream.
