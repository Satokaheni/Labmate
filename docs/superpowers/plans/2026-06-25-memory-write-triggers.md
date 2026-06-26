# Memory Write Triggers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enhance Labmate's memory write system with provenance tracking, retrieval-based importance boosting, TTL/decay, critic validation, and semantic pre-filtering for deduplication.

**Architecture:** The existing pipeline (extract → self_edit → apply_edits, plus on_task_complete and write_reflections triggers) is complete. This plan adds: (1) source provenance on every write; (2) importance boost when memories are retrieved; (3) time-based expiry proportional to importance; (4) optional critic LLM pass before committing; (5) vector-pre-filtered self_edit to reduce LLM prompt size and improve dedup precision.

**Tech Stack:** Python, asyncio, Motor (async MongoDB), chromadb (async), litellm (Gemma 4 31B), redis.asyncio, pytest + pytest-asyncio

---

## Pre-flight: ground truth observed in the codebase (read before starting)

These facts were verified against the current source and the new code below depends on them:

- `services/orchestrator/storage_manager.py::StorageManager.store_memory()` currently **drops** `importance` and `source` — it only persists `session_id, fact, embedding_text, valid_from, valid_to, supersedes, created_at, outbox`. Task 1 fixes this so provenance/importance actually survive the write.
- `services/orchestrator/outbox_worker.py::OutboxWorker.process_once()` projects only `{session_id, valid_to}` into Chroma metadata. Task 1 extends the projected metadata to include `source` and `importance` so retrieval can display/boost them.
- `services/orchestrator/storage_manager.py::StorageManager.search_memories()` returns `[{id, fact, metadata, distance}]`; `metadata` carries whatever the outbox projected.
- `services/orchestrator/memory_consolidator.py::MemoryConsolidator._memory_dict()` currently returns `{session_id, fact, importance, embedding_text}` with **no** `source`.
- `services/memory/context_manager.py::ContextManager.build_context()` calls `self.hybrid_retrieve(...)` which returns `[{id, text, score}]`. The importance boost integrates here (Task 2). `ContextManager.__init__` takes `(redis, mongo_db, chroma_cols, embedder, budget=None)` — Task 2 adds an optional `storage` hook so it can call back into the orchestrator's source-of-truth boost.
- Tests live under `tests/services/orchestrator/` and use the `storage`, `mock_mongo`, `mock_chroma`, `mock_redis` fixtures from `tests/services/orchestrator/conftest.py`. The mock collection auto-creates AsyncMock collections; `mock_mongo._collections[name]` is the test hook. `pytestmark = [pytest.mark.mocked, pytest.mark.asyncio]` is the file-level convention.
- `services/memory/` tests live under `services/memory/tests/`.
- All litellm calls MUST pass `api_key="not-needed"` and `extra_body={"thinking_budget_tokens": N}`. The consolidator routes every call through `MemoryConsolidator._complete()`, which already does this — reuse it, never call `litellm.acompletion` directly in new code.
- Logging to stderr only; no `print`.

Run all orchestrator tests from the repo root with the package import path:

```bash
cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/orchestrator/ -q
```

---

## Task 1 — Provenance tracking (`source` on every memory write)

**Files**
- Modify: `/Users/zachstallbohm/Work/Labmate/services/orchestrator/memory_consolidator.py`
- Modify: `/Users/zachstallbohm/Work/Labmate/services/orchestrator/storage_manager.py`
- Modify: `/Users/zachstallbohm/Work/Labmate/services/orchestrator/outbox_worker.py`
- Test: `/Users/zachstallbohm/Work/Labmate/tests/services/orchestrator/test_memory_provenance.py`

**Source taxonomy** (the only allowed values): `"user_stated"`, `"tool_output"`, `"agent_generated"`, `"compaction_reflection"`. `write_reflections()` already writes `"compaction_reflection"`; `on_task_complete()` already writes `"task_reflection"` — leave that value as-is (it is a legitimate fifth provenance and out of scope to rename).

### Steps

- [ ] Add a `_VALID_SOURCES` constant and a default to `memory_consolidator.py` (top of file, after the existing module constants near line 13):

```python
_VALID_SOURCES = {
    "user_stated",
    "tool_output",
    "agent_generated",
    "compaction_reflection",
}
_DEFAULT_SOURCE = "agent_generated"


def _normalize_source(value) -> str:
    """Coerce an LLM-supplied source string to the allowed taxonomy."""
    if isinstance(value, str) and value.strip() in _VALID_SOURCES:
        return value.strip()
    return _DEFAULT_SOURCE
```

- [ ] Update `_EXTRACT_PROMPT` (currently lines 85-96) so the LLM classifies the source. Replace the JSON-shape line and add the taxonomy instruction:

```python
_EXTRACT_PROMPT = (
    "You are a memory extractor. From the conversation episodes below, extract "
    "atomic, self-contained facts worth keeping long-term.\n"
    "Include: user preferences, decisions, entities (people/papers/tools), "
    "constraints, failures and lessons, AND agent confirmations/recommendations.\n"
    "Omit: greetings, progress updates, ephemeral chit-chat, and anything already "
    "implicit in stable context.\n"
    "For each fact, assign an importance (1=trivial, 3=standard preference, "
    "5=safety-critical or identity-level). Higher importance slows decay.\n"
    "For each fact, classify its SOURCE as exactly one of: "
    '"user_stated" (the user asserted it), '
    '"tool_output" (it came from a tool/search/file result), '
    '"agent_generated" (an agent inferred or recommended it).\n'
    'Return STRICT JSON: '
    '[{{"fact": str, "importance": int, "source": str}}].\n\n'
    "EPISODES:\n{episodes}"
)
```

- [ ] In `_extract_memories()` (lines 158-183), carry `source` through the parse loop. Change the per-fact append (currently `result.append({"fact": m["fact"], "importance": imp})`) to:

```python
                result.append({
                    "fact": m["fact"],
                    "importance": imp,
                    "source": _normalize_source(m.get("source")),
                })
```

- [ ] Change `_memory_dict()` (lines 201-208) to accept and persist `source`:

```python
    def _memory_dict(self, session_id: str, m: dict, source: str | None = None) -> dict:
        """Build the memory document stored in Chroma/Mongo."""
        return {
            "session_id": session_id,
            "fact": m["fact"],
            "importance": m.get("importance", 3),
            "embedding_text": m["fact"],
            "source": _normalize_source(source if source is not None else m.get("source")),
        }
```

- [ ] Update `_apply_edits()` (lines 210-218) to pass source through for both add and update:

```python
    async def _apply_edits(self, session_id: str, edits: dict) -> None:
        for m in edits.get("add", []):
            await self._semantic.upsert(self._s, self._memory_dict(session_id, m))
        for m in edits.get("update", []):
            await self._semantic.supersede(
                self._s, m["id"], self._memory_dict(session_id, m)
            )
        for m in edits.get("delete", []):
            await self._s.close_memory(m["id"])
```

(No structural change is needed here — `_memory_dict` now reads `source` from each `m`, which `_extract_memories` populated. The line is listed so the implementer confirms `m` still flows through `_memory_dict`.)

- [ ] Persist `source` in `StorageManager.store_memory()` (`storage_manager.py` lines 131-151). Add the field to the inserted doc:

```python
    async def store_memory(self, memory: dict) -> str:
        """Insert one semantic fact + outbox marker in a single Mongo write.

        memory: {session_id, fact, importance?, source?, valid_from?, valid_to?, supersedes?}
        """
        doc = {
            "session_id": memory["session_id"],
            "fact": memory["fact"],
            "embedding_text": memory.get("embedding_text", memory["fact"]),
            "importance": memory.get("importance", 3),
            "source": memory.get("source", "agent_generated"),
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
```

- [ ] Project `source` and `importance` into Chroma metadata in `OutboxWorker.process_once()` (`outbox_worker.py` lines 41-46). Replace the `meta` dict:

```python
                meta = {
                    "session_id": doc.get("session_id"),
                    "valid_to": (doc.get("valid_to").isoformat()
                                 if isinstance(doc.get("valid_to"), datetime)
                                 else doc.get("valid_to")),
                    "source": doc.get("source"),
                    "importance": doc.get("importance"),
                }
                meta = {k: v for k, v in meta.items() if v is not None}
```

(The `None`-filter is required: Chroma rejects `None` metadata values, and episode docs have no `source`/`importance`.)

- [ ] Surface provenance to the model at retrieval time. In `StorageManager.search_memories()` (`storage_manager.py` lines 169-192), prefix each returned fact with its source tag so whatever assembles the prompt shows where the memory came from. Replace the `out.append({...})` block (currently lines 186-191):

```python
            src = meta.get("source")
            fact_text = docs[i] if i < len(docs) else ""
            display = f"[{src}] {fact_text}" if src else fact_text
            out.append({
                "id": _id,
                "fact": display,
                "raw_fact": fact_text,
                "metadata": meta,
                "distance": dists[i] if i < len(dists) else None,
            })
```

(`raw_fact` preserves the untagged text for callers that need it — e.g. `SemanticMemory.search` feeding `_gather_neighbors` in Task 5, which should reason over the bare fact, not the tag. Update `_gather_neighbors` in Task 5 to read `h.get("raw_fact") or h.get("fact", "")` accordingly — see Task 5's note.)

- [ ] Write the test file `tests/services/orchestrator/test_memory_provenance.py`:

```python
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

pytestmark = [pytest.mark.mocked, pytest.mark.asyncio]


async def test_memory_dict_normalizes_source(storage):
    from services.orchestrator.memory_consolidator import MemoryConsolidator

    mc = MemoryConsolidator(storage, llm=AsyncMock())
    # explicit valid source is preserved
    d = mc._memory_dict("s1", {"fact": "x", "importance": 2}, source="user_stated")
    assert d["source"] == "user_stated"
    # invalid source falls back to default
    d2 = mc._memory_dict("s1", {"fact": "y", "source": "garbage"})
    assert d2["source"] == "agent_generated"


async def test_extract_carries_source(storage):
    from services.orchestrator.memory_consolidator import MemoryConsolidator

    fake_llm = AsyncMock(
        return_value='[{"fact":"user likes dark mode","importance":3,"source":"user_stated"}]'
    )
    mc = MemoryConsolidator(storage, llm=fake_llm)
    out = await mc._extract_memories([{"content": "I prefer dark mode"}])
    assert out == [{"fact": "user likes dark mode", "importance": 3, "source": "user_stated"}]


async def test_store_memory_persists_source_and_importance(storage, mock_mongo):
    await storage.store_memory({
        "session_id": "s1",
        "fact": "f",
        "importance": 5,
        "source": "tool_output",
    })
    doc = mock_mongo._collections["memories"].insert_one.await_args.args[0]
    assert doc["source"] == "tool_output"
    assert doc["importance"] == 5


async def test_apply_edits_passes_source_to_store(storage, mock_mongo):
    from services.orchestrator.memory_consolidator import MemoryConsolidator

    mc = MemoryConsolidator(storage, llm=AsyncMock())
    await mc._apply_edits("s1", {
        "add": [{"fact": "a", "importance": 3, "source": "user_stated"}],
        "update": [],
        "delete": [],
    })
    doc = mock_mongo._collections["memories"].insert_one.await_args.args[0]
    assert doc["source"] == "user_stated"


async def test_outbox_projects_source_and_filters_none(storage, mock_mongo, mock_chroma):
    from services.orchestrator.outbox_worker import OutboxWorker
    from bson import ObjectId

    oid = ObjectId()
    mem_doc = {
        "_id": oid, "session_id": "s1", "fact": "f", "source": "tool_output",
        "importance": 4, "valid_to": None,
        "outbox": {"kind": "memory_vector", "processed": False},
    }

    class _Cur:
        def __init__(self, docs): self._docs = docs
        def limit(self, n): return self
        def __aiter__(self):
            async def g():
                for d in self._docs:
                    yield d
            return g()

    mock_mongo._collections["memories"].find = lambda q: _Cur([mem_doc])
    mock_mongo._collections["episodes"].find = lambda q: _Cur([])

    worker = OutboxWorker(storage)
    await worker.process_once()

    meta = mock_chroma._collection.upsert.await_args.kwargs["metadatas"][0]
    assert meta["source"] == "tool_output"
    assert meta["importance"] == 4
    assert "valid_to" not in meta  # None filtered out


async def test_search_memories_tags_fact_with_source(storage, mock_chroma):
    mock_chroma._collection.query.return_value = {
        "ids": [["m1"]],
        "documents": [["user prefers dark mode"]],
        "metadatas": [[{"session_id": "s1", "source": "user_stated"}]],
        "distances": [[0.1]],
    }
    out = await storage.search_memories("preferences", top_k=1)
    assert out[0]["fact"] == "[user_stated] user prefers dark mode"
    assert out[0]["raw_fact"] == "user prefers dark mode"


async def test_search_memories_no_source_no_tag(storage, mock_chroma):
    mock_chroma._collection.query.return_value = {
        "ids": [["m1"]],
        "documents": [["a fact"]],
        "metadatas": [[{"session_id": "s1"}]],  # no source
        "distances": [[0.1]],
    }
    out = await storage.search_memories("q", top_k=1)
    assert out[0]["fact"] == "a fact"
    assert out[0]["raw_fact"] == "a fact"
```

- [ ] Run the tests:

```bash
cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/orchestrator/test_memory_provenance.py -q
```

Expected output: `7 passed`.

- [ ] Run the existing consolidator tests to confirm no regression:

```bash
cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/orchestrator/test_memory_consolidator.py tests/services/orchestrator/test_storage_manager.py -q
```

Expected output: all pass (existing test `test_apply_edits_routes_through_outbox` still passes — `insert_one.await_count == 2`, `update_one.await_count == 2`).

- [ ] Commit:

```bash
cd /Users/zachstallbohm/Work/Labmate && git add services/orchestrator/memory_consolidator.py services/orchestrator/storage_manager.py services/orchestrator/outbox_worker.py tests/services/orchestrator/test_memory_provenance.py && git commit -m "feat(memory): track provenance source on every memory write

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2 — Importance boost on retrieval

**Files**
- Modify: `/Users/zachstallbohm/Work/Labmate/services/orchestrator/storage_manager.py`
- Modify: `/Users/zachstallbohm/Work/Labmate/services/memory/context_manager.py`
- Test: `/Users/zachstallbohm/Work/Labmate/tests/services/orchestrator/test_importance_boost.py`
- Test: `/Users/zachstallbohm/Work/Labmate/services/memory/tests/test_boost_on_retrieve.py`

**Design:** Importance lives on the MongoDB source of truth (Task 1). The boost increments it there and re-opens the outbox so the OutboxWorker re-projects the new importance into Chroma. The `ContextManager` (memory service) gets an optional `storage` hook so `build_context` can boost the IDs returned by `hybrid_retrieve` without the memory service importing the orchestrator. Boost is best-effort and fire-and-forget — it must never block or fail retrieval.

### Steps

- [ ] Add `boost_memory_importance()` to `StorageManager` (`storage_manager.py`), placed right after `close_memory()` (after line 166):

```python
    async def boost_memory_importance(self, memory_id: str, delta: float = 0.1) -> None:
        """Increment a memory's importance (capped at 5.0) and re-project to Chroma.

        Called when a memory is retrieved into context: frequently-used memories
        become more durable. Best-effort — bad/missing ids are ignored. Re-opens
        the outbox so the OutboxWorker refreshes Chroma metadata + the TTL on the
        next sweep (see decay task). Importance is floored at 1.0 to stay in band.
        """
        from bson import ObjectId
        try:
            oid = ObjectId(memory_id)
        except Exception:
            return
        doc = await self._db[MEMORIES].find_one({"_id": oid}, {"importance": 1})
        if not doc:
            return
        current = doc.get("importance", 3)
        try:
            new_importance = min(5.0, float(current) + float(delta))
        except (TypeError, ValueError):
            return
        await self._db[MEMORIES].update_one(
            {"_id": oid},
            {"$set": {
                "importance": new_importance,
                "outbox.processed": False,
                "outbox.processed_at": None,
            }},
        )
```

- [ ] Add the optional `storage` hook to `ContextManager.__init__` (`services/memory/context_manager.py`, lines 60-73). Add the parameter and store it:

```python
    def __init__(
        self,
        redis,
        mongo_db,
        chroma_cols: dict,
        embedder,
        budget: ContextBudget | None = None,
        storage=None,
    ) -> None:
        self.redis  = redis
        self.db     = mongo_db
        self.chroma = chroma_cols
        self.embed  = embedder
        self.budget = budget or ContextBudget()
        self.storage = storage  # orchestrator StorageManager hook for importance boost
```

- [ ] Boost retrieved memories in `build_context()` (`context_manager.py`, after the `rag_chunks = await self.hybrid_retrieve(...)` line, currently line 96). Insert:

```python
        rag_chunks = await self.hybrid_retrieve(current_task, token_budget=rag_budget)
        await self._boost_retrieved(rag_chunks)
        rag_text   = "\n\n".join(c["text"] for c in rag_chunks)
```

- [ ] Add the `_boost_retrieved()` helper method to `ContextManager` (place it directly after `build_context`, before `_trim_to_budget`):

```python
    async def _boost_retrieved(self, chunks: list[dict]) -> None:
        """Increment importance on every retrieved memory (best-effort, non-blocking).

        Fire-and-forget: a boost failure must never break context assembly.
        No-op when no storage hook is wired (e.g. unit tests of pure retrieval).
        """
        if not self.storage or not chunks:
            return
        for c in chunks:
            cid = c.get("id")
            if not cid:
                continue
            try:
                await self.storage.boost_memory_importance(cid, delta=0.1)
            except Exception:
                _logger.debug("importance boost failed for %s", cid)
```

- [ ] Write `tests/services/orchestrator/test_importance_boost.py`:

```python
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

pytestmark = [pytest.mark.mocked, pytest.mark.asyncio]

_OID = "507f1f77bcf86cd799439011"


async def test_boost_increments_and_caps(storage, mock_mongo):
    mem = mock_mongo._collections["memories"]
    mem.find_one = AsyncMock(return_value={"importance": 4.95})
    await storage.boost_memory_importance(_OID, delta=0.1)
    update = mem.update_one.await_args.args[1]["$set"]
    assert update["importance"] == 5.0  # capped


async def test_boost_reopens_outbox(storage, mock_mongo):
    mem = mock_mongo._collections["memories"]
    mem.find_one = AsyncMock(return_value={"importance": 3})
    await storage.boost_memory_importance(_OID)
    update = mem.update_one.await_args.args[1]["$set"]
    assert update["importance"] == 3.1
    assert update["outbox.processed"] is False


async def test_boost_ignores_bad_id(storage, mock_mongo):
    mem = mock_mongo._collections["memories"]
    mem.update_one.reset_mock()
    await storage.boost_memory_importance("not-an-objectid")
    mem.update_one.assert_not_awaited()


async def test_boost_ignores_missing_memory(storage, mock_mongo):
    mem = mock_mongo._collections["memories"]
    mem.find_one = AsyncMock(return_value=None)
    mem.update_one.reset_mock()
    await storage.boost_memory_importance(_OID)
    mem.update_one.assert_not_awaited()
```

- [ ] Write `services/memory/tests/test_boost_on_retrieve.py`:

```python
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
```

- [ ] Run both test files:

```bash
cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/orchestrator/test_importance_boost.py services/memory/tests/test_boost_on_retrieve.py -q
```

Expected output: `7 passed`.

- [ ] Run the memory context-manager regression suite:

```bash
cd /Users/zachstallbohm/Work/Labmate && python -m pytest services/memory/tests/test_context_manager.py services/memory/tests/test_hybrid_retrieve.py -q
```

Expected output: all pass (the new `storage` param defaults to `None`, so existing constructions are unaffected).

- [ ] Commit:

```bash
cd /Users/zachstallbohm/Work/Labmate && git add services/orchestrator/storage_manager.py services/memory/context_manager.py tests/services/orchestrator/test_importance_boost.py services/memory/tests/test_boost_on_retrieve.py && git commit -m "feat(memory): boost importance when a memory is retrieved into context

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3 — Importance-based TTL / decay

**Files**
- Modify: `/Users/zachstallbohm/Work/Labmate/services/orchestrator/storage_manager.py`
- Modify: `/Users/zachstallbohm/Work/Labmate/services/orchestrator/db_indexes.py`
- Test: `/Users/zachstallbohm/Work/Labmate/tests/services/orchestrator/test_memory_decay.py`

**Design:** `expires_at` is computed at write time from importance. importance≥5 → never (`None`). A background sweep closes past-due, still-valid memories via `close_memory()` (which re-opens the outbox so Chroma reflects the closure). The sweep runs once per hour per active session, driven from the orchestrator's existing session loop.

| importance | TTL       |
|-----------:|-----------|
| 1          | 30 days   |
| 2          | 90 days   |
| 3          | 365 days  |
| 4          | 3 years (1095 days) |
| ≥5         | never (`expires_at=None`) |

### Steps

- [ ] Add the TTL table + helper to `storage_manager.py` (module level, after `_utcnow`, ~line 29):

```python
from datetime import timedelta

# Importance -> days-until-expiry. importance >= 5 never expires.
_TTL_DAYS = {1: 30, 2: 90, 3: 365, 4: 1095}


def _expires_at(importance, now: datetime | None = None) -> datetime | None:
    """Compute expiry from importance. Returns None for never-expiring (>=5)."""
    try:
        imp = int(round(float(importance)))
    except (TypeError, ValueError):
        imp = 3
    days = _TTL_DAYS.get(imp)
    if days is None:  # importance >= 5 (or <1 clamped); 5 = permanent
        if imp >= 5:
            return None
        days = _TTL_DAYS[1]  # clamp anything below 1 to the shortest TTL
    return (now or _utcnow()) + timedelta(days=days)
```

- [ ] Set `expires_at` in `store_memory()` (the doc built in Task 1). Add the field to the inserted doc (after `created_at`):

```python
            "created_at": _utcnow(),
            "expires_at": _expires_at(memory.get("importance", 3)),
```

- [ ] Add `decay_expired_memories()` to `StorageManager`, after `boost_memory_importance()`:

```python
    async def decay_expired_memories(self, session_id: str, now: datetime | None = None) -> int:
        """Close all currently-valid memories for a session whose expires_at is past.

        Returns the number of memories closed. Calls close_memory() per hit so the
        outbox re-projection marks them closed in Chroma. Idempotent: already-closed
        memories (valid_to set) are excluded by the query.
        """
        cutoff = now or _utcnow()
        cursor = self._db[MEMORIES].find(
            {
                "session_id": session_id,
                "valid_to": None,
                "expires_at": {"$ne": None, "$lte": cutoff},
            },
            {"_id": 1},
        )
        ids = [doc["_id"] async for doc in cursor]
        for _id in ids:
            await self.close_memory(str(_id), valid_to=cutoff)
        if ids:
            logger.info("decayed %d expired memories for session=%s", len(ids), session_id)
        return len(ids)
```

- [ ] Add an `expires_at` index in `db_indexes.py` `ensure_indexes()` (after the `memories` index on line 16):

```python
    await db["memories"].create_index([("session_id", 1), ("valid_to", 1), ("expires_at", 1)])
```

- [ ] Wire the hourly sweep into the orchestrator session lifecycle. In `services/orchestrator/main.py`, locate the per-task entry block that already references `storage.consolidator` (near the auto-compact block around line 342) and add a throttled sweep. Use the Redis working cache as the throttle so it is at-most-once-per-hour per session:

```python
            # Importance-based decay sweep (at most once/hour per session)
            try:
                if await storage.cache_get(f"decay_swept:{session_id}") is None:
                    asyncio.create_task(storage.decay_expired_memories(session_id))
                    await storage.cache_set(f"decay_swept:{session_id}", "1", ttl=3600)
            except Exception:
                pass  # decay is best-effort; never block task execution
```

(The 3600s cache key acts as the per-session hourly gate; `cache_set` already prefixes `cache:` so the throttle key is isolated.)

- [ ] Write `tests/services/orchestrator/test_memory_decay.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

pytestmark = [pytest.mark.mocked, pytest.mark.asyncio]


def _now():
    return datetime(2026, 6, 25, tzinfo=timezone.utc)


def test_expires_at_table():
    from services.orchestrator.storage_manager import _expires_at

    n = _now()
    assert _expires_at(1, n) == n + timedelta(days=30)
    assert _expires_at(2, n) == n + timedelta(days=90)
    assert _expires_at(3, n) == n + timedelta(days=365)
    assert _expires_at(4, n) == n + timedelta(days=1095)
    assert _expires_at(5, n) is None      # never expires
    assert _expires_at(7, n) is None      # clamps high to never
    assert _expires_at("bad", n) == n + timedelta(days=365)  # default importance 3


async def test_store_memory_sets_expires_at(storage, mock_mongo):
    await storage.store_memory({"session_id": "s1", "fact": "f", "importance": 1})
    doc = mock_mongo._collections["memories"].insert_one.await_args.args[0]
    assert doc["expires_at"] is not None
    # importance 5 -> never expires
    await storage.store_memory({"session_id": "s1", "fact": "g", "importance": 5})
    doc5 = mock_mongo._collections["memories"].insert_one.await_args.args[0]
    assert doc5["expires_at"] is None


async def test_decay_closes_past_due(storage, mock_mongo):
    from bson import ObjectId

    expired = [{"_id": ObjectId()}, {"_id": ObjectId()}]

    class _Cur:
        def __init__(self, docs): self._docs = docs
        def __aiter__(self):
            async def g():
                for d in self._docs:
                    yield d
            return g()

    mock_mongo._collections["memories"].find = lambda q, proj=None: _Cur(expired)
    storage.close_memory = AsyncMock()

    n = _now()
    closed = await storage.decay_expired_memories("s1", now=n)
    assert closed == 2
    assert storage.close_memory.await_count == 2
    # close called with the cutoff timestamp
    assert storage.close_memory.await_args.kwargs["valid_to"] == n


async def test_decay_query_excludes_never_and_closed(storage, mock_mongo):
    captured = {}

    class _Cur:
        def __aiter__(self):
            async def g():
                if False:
                    yield None
            return g()

    def _find(q, proj=None):
        captured["q"] = q
        return _Cur()

    mock_mongo._collections["memories"].find = _find
    await storage.decay_expired_memories("s1", now=_now())
    q = captured["q"]
    assert q["valid_to"] is None
    assert q["expires_at"]["$ne"] is None  # never-expiring excluded
```

- [ ] Run the tests:

```bash
cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/orchestrator/test_memory_decay.py -q
```

Expected output: `4 passed`.

- [ ] Confirm Task 1/2 tests still pass (store_memory shape changed):

```bash
cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/orchestrator/test_memory_provenance.py tests/services/orchestrator/test_storage_manager.py -q
```

Expected output: all pass.

- [ ] Commit:

```bash
cd /Users/zachstallbohm/Work/Labmate && git add services/orchestrator/storage_manager.py services/orchestrator/db_indexes.py services/orchestrator/main.py tests/services/orchestrator/test_memory_decay.py && git commit -m "feat(memory): importance-based TTL with hourly decay sweep

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 4 — Critic validation pass before commit

**Files**
- Modify: `/Users/zachstallbohm/Work/Labmate/services/orchestrator/memory_consolidator.py`
- Test: `/Users/zachstallbohm/Work/Labmate/tests/services/orchestrator/test_memory_critic.py`

**Design:** Before `_apply_edits` commits, an optional fast LLM critic checks each ADD/UPDATE candidate against the source episodes. Invalid candidates are dropped and logged; DELETE/NOOP are passed through unchecked (they remove or no-op, not introduce, content). Off by default for speed; enabled via `critic_enabled=True` on the consolidator (constructor flag, also overridable per-session by `maybe_consolidate`). The critic reuses `_complete()` so the litellm conventions hold automatically.

### Steps

- [ ] Add the critic prompt to `memory_consolidator.py` after `_TASK_REFLECTION_PROMPT` (~line 125):

```python
# Critic validation: run before committing ADD/UPDATE candidates.
_CRITIC_PROMPT = (
    "You are a strict memory critic. Decide whether the CANDIDATE memory should be "
    "committed, given the SOURCE episodes it was derived from.\n"
    "Reject (INVALID) if the candidate: contradicts the source, introduces facts not "
    "supported by the source (hallucination), or uses the wrong operation for its "
    "content.\n"
    "Accept (VALID) if it is faithful to the source and self-contained.\n"
    "OPERATION: {op}\n"
    "CANDIDATE: {fact}\n"
    "SOURCE EPISODES:\n{episodes}\n\n"
    'Return STRICT JSON: {{"verdict": "VALID"|"INVALID", "reason": str}}.'
)
```

- [ ] Add the `critic_enabled` flag to `MemoryConsolidator.__init__` (lines 129-134):

```python
    def __init__(
        self,
        storage,
        lm_base_url: str | None = None,
        llm=None,
        critic_enabled: bool = False,
    ) -> None:
        self._s = storage
        self._base = lm_base_url or GEMMA_BASE
        self._llm = llm  # injectable async callable(messages) -> str; defaults to litellm
        self._episodic = EpisodicMemory()
        self._semantic = SemanticMemory()
        self._critic_enabled = critic_enabled
```

- [ ] Add the per-candidate critic method (after `_self_edit`, before `_memory_dict`):

```python
    async def _critique(self, op: str, fact: str, episodes_text: str) -> bool:
        """Return True if the candidate is VALID. Fail-open on parse/LLM error.

        Fail-open (treat as VALID on error) is deliberate: a flaky critic must not
        silently drop legitimate memories. Genuine rejections are explicit INVALID.
        """
        try:
            raw = await self._complete(_CRITIC_PROMPT.format(
                op=op, fact=fact, episodes=episodes_text[:4_000],
            ))
            data = self._parse_json(raw)
        except (json.JSONDecodeError, ValueError):
            logger.warning("critic: non-JSON response, accepting candidate")
            return True
        verdict = str(data.get("verdict", "VALID")).strip().upper()
        if verdict == "INVALID":
            logger.info("critic rejected (%s): %s — %s",
                        op, fact[:60], data.get("reason", ""))
            return False
        return True
```

- [ ] Add a filter that runs the critic over an edits dict (after `_critique`):

```python
    async def _filter_edits(self, edits: dict, episodes_text: str) -> dict:
        """Drop ADD/UPDATE candidates the critic marks INVALID. DELETE/NOOP pass through."""
        if not self._critic_enabled:
            return edits
        kept_add = []
        for m in edits.get("add", []):
            if await self._critique("ADD", m.get("fact", ""), episodes_text):
                kept_add.append(m)
        kept_update = []
        for m in edits.get("update", []):
            if await self._critique("UPDATE", m.get("fact", ""), episodes_text):
                kept_update.append(m)
        return {
            "add": kept_add,
            "update": kept_update,
            "delete": edits.get("delete", []),
        }
```

- [ ] Wire the critic into `maybe_consolidate()` (lines 284-301). Build the episodes text once and filter before applying. Replace the body from the `edits = await self._self_edit(...)` line onward:

```python
        episodes = await self._episodic.get_recent(self._s, session_id)
        candidates = await self._extract_memories(episodes)
        if not candidates:
            return False
        existing = await self._semantic.search(self._s, candidates[0]["fact"], top_k=10)
        edits = await self._self_edit(candidates, existing)
        episodes_text = "\n".join(f"- {e.get('content', '')}" for e in episodes)
        edits = await self._filter_edits(edits, episodes_text)
        await self._apply_edits(session_id, edits)
        logger.info("consolidated session=%s add=%d update=%d delete=%d",
                    session_id, len(edits["add"]), len(edits["update"]), len(edits["delete"]))
        return True
```

- [ ] Write `tests/services/orchestrator/test_memory_critic.py`:

```python
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

pytestmark = [pytest.mark.mocked, pytest.mark.asyncio]


async def test_critic_disabled_by_default_passes_all(storage):
    from services.orchestrator.memory_consolidator import MemoryConsolidator

    mc = MemoryConsolidator(storage, llm=AsyncMock())  # critic off
    edits = {"add": [{"fact": "a"}], "update": [{"id": "u", "fact": "b"}], "delete": []}
    out = await mc._filter_edits(edits, "episodes")
    assert out == edits  # untouched


async def test_critic_drops_invalid_add(storage):
    from services.orchestrator.memory_consolidator import MemoryConsolidator

    fake_llm = AsyncMock(return_value='{"verdict":"INVALID","reason":"hallucinated"}')
    mc = MemoryConsolidator(storage, llm=fake_llm, critic_enabled=True)
    edits = {"add": [{"fact": "made up fact"}], "update": [], "delete": [{"id": "d"}]}
    out = await mc._filter_edits(edits, "source episodes")
    assert out["add"] == []           # rejected
    assert out["delete"] == [{"id": "d"}]  # delete passes through unchecked


async def test_critic_keeps_valid(storage):
    from services.orchestrator.memory_consolidator import MemoryConsolidator

    fake_llm = AsyncMock(return_value='{"verdict":"VALID","reason":"ok"}')
    mc = MemoryConsolidator(storage, llm=fake_llm, critic_enabled=True)
    edits = {"add": [{"fact": "true fact"}], "update": [], "delete": []}
    out = await mc._filter_edits(edits, "source episodes")
    assert out["add"] == [{"fact": "true fact"}]


async def test_critic_fails_open_on_bad_json(storage):
    from services.orchestrator.memory_consolidator import MemoryConsolidator

    mc = MemoryConsolidator(storage, llm=AsyncMock(return_value="not json"), critic_enabled=True)
    assert await mc._critique("ADD", "fact", "episodes") is True


async def test_critique_returns_false_on_invalid(storage):
    from services.orchestrator.memory_consolidator import MemoryConsolidator

    fake_llm = AsyncMock(return_value='{"verdict":"INVALID","reason":"contradicts"}')
    mc = MemoryConsolidator(storage, llm=fake_llm, critic_enabled=True)
    assert await mc._critique("UPDATE", "fact", "episodes") is False
```

- [ ] Run the tests:

```bash
cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/orchestrator/test_memory_critic.py -q
```

Expected output: `5 passed`.

- [ ] Confirm consolidator regression (constructor signature changed — new param is keyword-defaulted, existing calls unaffected):

```bash
cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/orchestrator/test_memory_consolidator.py -q
```

Expected output: all pass.

- [ ] Commit:

```bash
cd /Users/zachstallbohm/Work/Labmate && git add services/orchestrator/memory_consolidator.py tests/services/orchestrator/test_memory_critic.py && git commit -m "feat(memory): optional critic validation pass before committing writes

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 5 — Semantic pre-filtering in `_self_edit`

**Files**
- Modify: `/Users/zachstallbohm/Work/Labmate/services/orchestrator/memory_consolidator.py`
- Test: `/Users/zachstallbohm/Work/Labmate/tests/services/orchestrator/test_self_edit_prefilter.py`

**Design:** Instead of sending all existing memories to `_self_edit`, retrieve only the top-3 nearest existing memories per new candidate (via `SemanticMemory.search`, which wraps `storage.search_memories` → Chroma), deduplicate by id, and pass only those neighbors. Smaller prompt, sharper dedup. `maybe_consolidate` is updated to use the pre-filtered neighbor set.

**Depends on Task 1:** `search_memories` now returns a `raw_fact` (untagged) alongside the `[source]`-prefixed `fact`. `_gather_neighbors` reads `raw_fact` so `_self_edit` reasons over bare facts, not provenance tags. **Depends on Task 4:** this task's `maybe_consolidate` retains the `_filter_edits` critic call introduced in Task 4 — apply Task 5's full method last.

### Steps

- [ ] Add `_gather_neighbors()` to `MemoryConsolidator` (after `_self_edit`, before `_critique` from Task 4):

```python
    async def _gather_neighbors(self, candidates: list[dict], per_fact: int = 3) -> list[dict]:
        """Retrieve top-N existing memories near each candidate; dedupe by id.

        Replaces sending the full existing-memory set to _self_edit. For each new
        candidate we pull its nearest existing neighbors so _self_edit only reasons
        over plausibly-related facts. Returns [{id, fact}, ...] with unique ids.
        """
        seen: dict[str, dict] = {}
        for cand in candidates:
            fact = cand.get("fact", "")
            if not fact:
                continue
            hits = await self._semantic.search(self._s, fact, top_k=per_fact)
            for h in hits:
                hid = h.get("id")
                if hid and hid not in seen:
                    # prefer the untagged fact (search_memories prefixes a [source] tag
                    # onto "fact"); fall back to "fact" for callers that don't tag.
                    seen[hid] = {"id": hid, "fact": h.get("raw_fact") or h.get("fact", "")}
        return list(seen.values())
```

- [ ] Rewrite `maybe_consolidate()` (lines 284-301) to use neighbor pre-filtering instead of the single `top_k=10` search. Full replacement of the method body after the interval gate:

```python
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
        # Semantic pre-filter: only the nearest existing neighbors per candidate
        existing = await self._gather_neighbors(candidates, per_fact=3)
        edits = await self._self_edit(candidates, existing)
        episodes_text = "\n".join(f"- {e.get('content', '')}" for e in episodes)
        edits = await self._filter_edits(edits, episodes_text)
        await self._apply_edits(session_id, edits)
        logger.info("consolidated session=%s neighbors=%d add=%d update=%d delete=%d",
                    session_id, len(existing),
                    len(edits["add"]), len(edits["update"]), len(edits["delete"]))
        return True
```

(This supersedes the Task 4 edit to `maybe_consolidate` — apply Task 5's full version. The `_filter_edits` call from Task 4 is retained here.)

- [ ] Write `tests/services/orchestrator/test_self_edit_prefilter.py`:

```python
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

pytestmark = [pytest.mark.mocked, pytest.mark.asyncio]


async def test_gather_neighbors_dedupes_by_id(storage):
    from services.orchestrator.memory_consolidator import MemoryConsolidator

    mc = MemoryConsolidator(storage, llm=AsyncMock())

    async def fake_search(_storage, query, top_k=5):
        # both candidates return overlapping ids
        if "dog" in query:
            return [{"id": "m1", "fact": "has a dog"}, {"id": "m2", "fact": "likes pets"}]
        return [{"id": "m2", "fact": "likes pets"}, {"id": "m3", "fact": "has a cat"}]

    mc._semantic.search = fake_search
    neighbors = await mc._gather_neighbors(
        [{"fact": "user got a second dog"}, {"fact": "user adopted a cat"}], per_fact=3
    )
    ids = sorted(n["id"] for n in neighbors)
    assert ids == ["m1", "m2", "m3"]  # m2 deduped


async def test_gather_neighbors_skips_empty_facts(storage):
    from services.orchestrator.memory_consolidator import MemoryConsolidator

    mc = MemoryConsolidator(storage, llm=AsyncMock())
    calls = []

    async def fake_search(_storage, query, top_k=5):
        calls.append(query)
        return []

    mc._semantic.search = fake_search
    out = await mc._gather_neighbors([{"fact": ""}, {"fact": "real"}], per_fact=3)
    assert out == []
    assert calls == ["real"]  # empty-fact candidate skipped


async def test_gather_neighbors_uses_per_fact_top_k(storage):
    from services.orchestrator.memory_consolidator import MemoryConsolidator

    mc = MemoryConsolidator(storage, llm=AsyncMock())
    seen_top_k = []

    async def fake_search(_storage, query, top_k=5):
        seen_top_k.append(top_k)
        return []

    mc._semantic.search = fake_search
    await mc._gather_neighbors([{"fact": "a"}], per_fact=3)
    assert seen_top_k == [3]


async def test_maybe_consolidate_uses_neighbors(storage, mock_mongo):
    from services.orchestrator.memory_consolidator import (
        MemoryConsolidator, CONSOLIDATION_INTERVAL,
    )

    mc = MemoryConsolidator(storage, llm=AsyncMock())
    _ = storage._db["episodes"]
    mock_mongo._collections["episodes"].count_documents = AsyncMock(
        return_value=CONSOLIDATION_INTERVAL
    )

    class _Cur:
        def __init__(self, docs): self._docs = docs
        def sort(self, *_): return self
        def limit(self, n): return self
        def __aiter__(self):
            async def g():
                for d in self._docs:
                    yield d
            return g()

    mock_mongo._collections["episodes"].find = lambda q: _Cur([{"content": "ep"}])

    mc._extract_memories = AsyncMock(return_value=[{"fact": "f", "importance": 3}])
    mc._gather_neighbors = AsyncMock(return_value=[{"id": "n1", "fact": "old"}])
    mc._self_edit = AsyncMock(return_value={"add": [], "update": [], "delete": []})
    mc._apply_edits = AsyncMock()

    ran = await mc.maybe_consolidate("s1")
    assert ran is True
    mc._gather_neighbors.assert_awaited_once()
    # _self_edit received the neighbor set as the existing-memories arg
    assert mc._self_edit.await_args.args[1] == [{"id": "n1", "fact": "old"}]
```

- [ ] Run the tests:

```bash
cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/orchestrator/test_self_edit_prefilter.py -q
```

Expected output: `4 passed`.

- [ ] Run the full memory suite to confirm the whole pipeline holds together:

```bash
cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/orchestrator/test_memory_consolidator.py tests/services/orchestrator/test_memory_provenance.py tests/services/orchestrator/test_importance_boost.py tests/services/orchestrator/test_memory_decay.py tests/services/orchestrator/test_memory_critic.py tests/services/orchestrator/test_self_edit_prefilter.py -q
```

Expected output: all pass.

- [ ] Commit:

```bash
cd /Users/zachstallbohm/Work/Labmate && git add services/orchestrator/memory_consolidator.py tests/services/orchestrator/test_self_edit_prefilter.py && git commit -m "feat(memory): vector pre-filter neighbors before self_edit for sharper dedup

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Final verification

- [ ] Run the entire orchestrator + memory test suites:

```bash
cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/orchestrator/ services/memory/tests/ -q
```

Expected output: all pass, no warnings about unhandled coroutines.

- [ ] Confirm no forbidden patterns slipped in (mirrors `test_no_tiktoken_import`):

```bash
cd /Users/zachstallbohm/Work/Labmate && grep -nE "tiktoken|PersistentClient|EphemeralClient|\.rpush\(|\.brpop\(|print\(" services/orchestrator/memory_consolidator.py services/orchestrator/storage_manager.py services/orchestrator/outbox_worker.py services/memory/context_manager.py || echo "clean"
```

Expected output: `clean`.

- [ ] Confirm every new litellm path passes the required kwargs (all new LLM calls go through `_complete`, which is unchanged and already correct):

```bash
cd /Users/zachstallbohm/Work/Labmate && grep -n "api_key\|thinking_budget_tokens" services/orchestrator/memory_consolidator.py
```

Expected output: the existing `_complete` lines showing `api_key="not-needed"` and `extra_body={"thinking_budget_tokens": 1024}`.

---

## Gherkin Scenarios

These scenarios describe expected system behavior at the feature level. Use them for acceptance testing and to verify the implementation matches intent.

```gherkin
Feature: Memory provenance tracking

  Scenario: LLM-extracted facts include a source classification
    Given conversation episodes containing user preferences
    When _extract_memories is called
    Then each returned fact has a source field
    And the source is one of "user_stated", "tool_output", or "agent_generated"

  Scenario: Invalid source strings are normalised to the default
    Given an LLM response with source "unknown_value"
    When _normalize_source is called
    Then "agent_generated" is returned

  Scenario: Memory document persists source and importance
    Given a memory dict with source "tool_output" and importance 5
    When store_memory is called
    Then the MongoDB document contains source "tool_output"
    And the document contains importance 5

  Scenario: Outbox worker projects source and importance into Chroma metadata
    Given a memory document in MongoDB with source "user_stated" and importance 4
    When the OutboxWorker processes the outbox entry
    Then the Chroma upsert metadata contains source "user_stated"
    And the Chroma upsert metadata contains importance 4

  Scenario: None metadata values are filtered before Chroma upsert
    Given a memory document with valid_to None
    When the OutboxWorker processes it
    Then valid_to does not appear in the Chroma metadata

  Scenario: Retrieved facts display their provenance tag
    Given a Chroma collection containing a memory tagged with source "user_stated"
    When search_memories is called
    Then the returned fact string is prefixed with "[user_stated]"
    And raw_fact contains the untagged text

  Scenario: Retrieved facts without a source tag are returned unmodified
    Given a Chroma collection with a memory that has no source field
    When search_memories is called
    Then fact and raw_fact are identical


Feature: Importance boost on memory retrieval

  Scenario: Retrieving a memory increments its importance
    Given a memory with importance 3.0 in MongoDB
    When boost_memory_importance is called with delta 0.1
    Then the memory's importance is updated to 3.1
    And the outbox is re-opened so the OutboxWorker re-projects to Chroma

  Scenario: Importance is capped at 5.0
    Given a memory with importance 4.95
    When boost_memory_importance is called with delta 0.1
    Then the importance stored is exactly 5.0

  Scenario: Invalid ObjectId is silently ignored
    Given a memory ID that is not a valid ObjectId string
    When boost_memory_importance is called
    Then no database update occurs
    And no exception is raised

  Scenario: Missing memory document is silently ignored
    Given a memory ID that does not exist in the database
    When boost_memory_importance is called
    Then no update_one is executed

  Scenario: build_context boosts each retrieved memory ID
    Given a ContextManager with a storage hook wired up
    And hybrid_retrieve returns two chunks with IDs "a" and "b"
    When build_context is called
    Then boost_memory_importance is called twice
    And once with id "a" and once with id "b"

  Scenario: Boost failure does not break context assembly
    Given boost_memory_importance raises a RuntimeError
    When build_context assembles context
    Then context assembly completes without raising
    And the returned context is not empty

  Scenario: No storage hook means boost is skipped silently
    Given a ContextManager with no storage hook
    When _boost_retrieved is called with chunks
    Then no exception is raised


Feature: Importance-based TTL and decay

  Scenario Outline: expires_at is computed from importance at write time
    Given a memory with importance <importance>
    When store_memory is called
    Then expires_at is <days> days from now

    Examples:
      | importance | days |
      | 1          | 30   |
      | 2          | 90   |
      | 3          | 365  |
      | 4          | 1095 |

  Scenario: Importance 5 memories never expire
    Given a memory with importance 5
    When store_memory is called
    Then expires_at is None

  Scenario: Expired memories are closed by the decay sweep
    Given 2 memories for session "s1" with expires_at in the past
    When decay_expired_memories is called with the current timestamp
    Then close_memory is called for each expired memory
    And 2 is returned

  Scenario: Never-expiring memories are excluded from the decay query
    Given a memory with expires_at None
    When the decay query runs for session "s1"
    Then the query filter requires expires_at $ne None

  Scenario: Already-closed memories are excluded from decay
    Given a memory with valid_to already set
    When the decay query runs
    Then the query filter requires valid_to is None

  Scenario: Hourly throttle prevents repeated sweeps per session
    Given a "decay_swept:{session_id}" cache key already set
    When the orchestrator processes a task for that session
    Then decay_expired_memories is not called again until the key expires


Feature: Critic validation before committing memory writes

  Scenario: Critic is off by default and all candidates pass through
    Given a MemoryConsolidator with default settings (critic_enabled=False)
    When _filter_edits is called with ADD candidates
    Then all candidates are returned unchanged

  Scenario: Critic rejects hallucinated ADD facts
    Given a MemoryConsolidator with critic_enabled=True
    And the LLM returns INVALID for an ADD candidate
    When _filter_edits is called
    Then the rejected candidate is removed from the add list

  Scenario: DELETE operations bypass the critic
    Given a MemoryConsolidator with critic_enabled=True
    And the LLM returns INVALID for an ADD candidate
    When _filter_edits is called
    Then DELETE operations are included in the output regardless

  Scenario: Critic accepts faithful facts
    Given a MemoryConsolidator with critic_enabled=True
    And the LLM returns VALID for every candidate
    When _filter_edits is called
    Then all candidates appear in the output

  Scenario: Critic fails open when the LLM returns malformed JSON
    Given a MemoryConsolidator with critic_enabled=True
    And the LLM returns text that is not valid JSON
    When _critique is called
    Then True is returned (fail open — accept the candidate)


Feature: Semantic pre-filtering for deduplication

  Scenario: _gather_neighbors deduplicates by memory ID across candidates
    Given two candidates each matching memory "m2" among their neighbors
    When _gather_neighbors is called with both candidates
    Then "m2" appears exactly once in the result

  Scenario: Candidates with empty fact strings are skipped
    Given a candidate with an empty fact string
    When _gather_neighbors is called
    Then no vector search is issued for the empty candidate

  Scenario: per_fact controls the top-k passed to each vector search
    Given a single candidate
    When _gather_neighbors is called with per_fact=3
    Then the semantic search top_k argument is 3

  Scenario: maybe_consolidate passes neighbor set as existing memories to _self_edit
    Given enough episodes to trigger consolidation
    And _gather_neighbors returns neighbors [m1, m2]
    When maybe_consolidate runs
    Then _self_edit is called with [m1, m2] as the existing-memories argument

  Scenario: Task 4 critic filter is retained in maybe_consolidate
    Given a MemoryConsolidator with critic_enabled=True
    When maybe_consolidate runs after gathering neighbors
    Then _filter_edits is called on the self_edit result before applying
```
