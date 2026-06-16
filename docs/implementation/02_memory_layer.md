# Implementation Plan — Memory Layer
# Labmate · services/orchestrator/memory/

**Version**: 1.0  
**Date**: 2026-06-16  
**Contracts referenced**: C (MongoDB), D (Chroma), E (Redis Streams) — see 00_contracts.md

---

## 1. What This Module Does

The memory layer is a pure Python module that lives inside the orchestrator container at
`services/orchestrator/memory/`. It is not a separate service and it is not accessed over
HTTP. The orchestrator imports and calls it directly.

The module abstracts all three storage backends behind two classes:

**`StorageManager`** — owns the three client singletons (Motor, Chroma AsyncHttpClient,
redis.asyncio). Provides the write path (messages, sessions, tool calls, outbox), the
search path (vector similarity resolved back to MongoDB source-of-truth), and the task
enqueue path (XADD to Redis Streams). Runs the outbox worker as a background asyncio task
that tails the MongoDB change stream and projects every new record to Chroma and Redis.

**`ContextManager`** — owns context assembly. Given a session ID and a current task string,
it pulls core memory from Redis, retrieves relevant chunks via hybrid RAG (BM25 + Chroma
dense vector → RRF fusion → FlagReranker cross-encoder), loads the sliding window of
recent turns from MongoDB, and assembles the full prompt that gets sent to vLLM. Every
token count passes through the Gemma AutoTokenizer. Hard sub-budgets are enforced per
slot. Runs a consolidation worker that extracts semantic facts from core memory and writes
them to Chroma off the agent's hot path.

The orchestrator calls these two classes for every agent turn. No other component in the
system touches MongoDB, Chroma, or Redis directly — all access goes through this module.

---

## 2. Dependencies

### Python packages

```toml
# pyproject.toml additions
motor              = ">=3.5"          # AsyncIOMotorClient (async MongoDB)
pymongo            = ">=4.9"          # BSON, ObjectId (pulled in by motor)
chromadb           = ">=0.5"          # AsyncHttpClient — never EphemeralClient or PersistentClient
redis              = ">=5.0"          # redis.asyncio, Streams (XADD/XREADGROUP/XACK/XAUTOCLAIM)
transformers       = ">=4.40"         # AutoTokenizer (Gemma SentencePiece) — NEVER tiktoken
sentence-transformers = ">=3.0"       # SentenceTransformer for dense embeddings (BAAI/bge-small-en-v1.5)
FlagEmbedding      = ">=1.2"          # FlagReranker (BAAI/bge-reranker-v2-m3) cross-encoder
rank-bm25          = ">=0.2"          # BM25Okapi for lexical first stage
pydantic           = ">=2.0"          # typed schemas
bson               = ">=0.5"          # ObjectId outside motor context
```

Do NOT add `tiktoken` to pyproject.toml. Its presence is a footgun — it will be imported
by accident. The Gemma tokenizer is the only tokenizer allowed in this module.

### Running services required

The following containers must be healthy before the orchestrator starts. The memory module
does not start them — Docker Compose handles that.

| Service | Container name | Port | Note |
|---------|---------------|------|------|
| MongoDB | `lm-mongodb` | 27017 | `MONGO_URI=mongodb://mongodb:27017/labmate` |
| Chroma | `lm-chroma` | 8000 | `CHROMA_URL=http://chroma:8000` — must be running as a standalone server |
| Redis | `lm-redis` | 6379 | `REDIS_URL=redis://redis:6379/0` |

Chroma must be started with `chroma run --host 0.0.0.0 --port 8000`. Never use
EphemeralClient or PersistentClient in-process.

---

## 3. File Structure

Create exactly these files. Do not create anything else.

```
services/orchestrator/
└── memory/
    ├── __init__.py          — Re-exports StorageManager, ContextManager, ContextBudget
    ├── tokenizer.py         — Gemma AutoTokenizer singleton + token_count()
    ├── storage.py           — StorageManager: MongoDB + Chroma + Redis clients, write_message(),
    │                          search_memory(), enqueue_task(), _run_outbox() background worker
    ├── vector.py            — Chroma collection helpers: get_or_create_collections(), upsert(), query()
    ├── queue.py             — Redis Stream helpers: ensure_consumer_group(), enqueue(), consume(),
    │                          reclaim_stale()
    └── context.py           — ContextManager: build_context(), hybrid_retrieve(),
                               consolidation_worker(), _trim_core_memory()
```

All imports between files use relative imports (`from .tokenizer import token_count`).

---

## 4. Interface Contracts

### 4.1 StorageManager public API

```python
class StorageManager:
    """Async context manager. Use as:
        async with StorageManager.from_env() as sm:
            ...
    """

    @classmethod
    def from_env(cls) -> "StorageManager":
        """Read MONGO_URI, CHROMA_URL, REDIS_URL from environment."""
        ...

    async def __aenter__(self) -> "StorageManager": ...
    async def __aexit__(self, *exc) -> None: ...

    # --- Write path ---

    async def create_session(
        self,
        session_id: str,
        goal: str,
        project_id: str = "",
    ) -> ObjectId:
        """Insert a sessions document. Idempotent (upsert on session_id)."""
        ...

    async def write_message(
        self,
        session_id: str,
        sequence: int,
        role: str,                      # "system" | "user" | "assistant" | "tool"
        content: str | None,
        token_count: int,
        tool_calls: list[dict] | None = None,
        tool_call_id: str | None = None,
    ) -> ObjectId:
        """
        Write message + outbox doc in ONE Motor session (atomic).
        Returns the new message ObjectId.
        The outbox worker picks up the outbox doc and projects to Chroma + Redis.
        """
        ...

    async def write_tool_call(
        self,
        session_id: str,
        message_id: ObjectId,
        tool_name: str,
        input_data: dict,
        output_data: dict,
        duration_ms: int,
    ) -> ObjectId:
        """Write a tool_calls document. No outbox — not projected to Chroma."""
        ...

    # --- Read path ---

    async def get_session(self, session_id: str) -> dict | None: ...

    async def get_messages(
        self,
        session_id: str,
        limit: int = 50,
        skip: int = 0,
    ) -> list[dict]:
        """Return messages in ascending sequence order."""
        ...

    async def search_memory(
        self,
        query_embedding: list[float],
        collection: str = "episodic",   # "episodic" | "semantic" | "procedural"
        k: int = 10,
        where: dict | None = None,      # Chroma metadata filter
    ) -> list[dict]:
        """
        Vector search in Chroma → resolve to MongoDB source-of-truth.
        Orphan Chroma points (no matching MongoDB doc) are silently dropped.
        """
        ...

    # --- Task queue ---

    async def enqueue_task(
        self,
        stream: str,                    # e.g. "lm:tasks"
        fields: dict,
        maxlen: int = 10_000,
    ) -> str:
        """XADD to a Redis Stream. Returns the entry ID (e.g. '1718-0')."""
        ...
```

### 4.2 ContextManager public API

```python
class ContextBudget:
    max_tokens: int = 8192
    completion_reserve: int = 700
    system_core_share: float = 0.25
    recent_turns_share: float = 0.30
    rag_share: float = 0.35
    summary_share: float = 0.10

    @property
    def effective_budget(self) -> int: ...
    def slot(self, share: float) -> int: ...


class AssembledContext:
    system_prompt: str
    core_memory: str
    recent_turns: str
    retrieved_context: str
    summary_buffer: str
    total_tokens: int

    def as_prompt(self) -> str:
        """Assemble: system → core → retrieved (head) → summary → recent (tail).
        Head + tail placement counters lost-in-the-middle degradation."""
        ...


class ContextManager:
    def __init__(
        self,
        redis,                          # redis.asyncio.Redis
        mongo_db,                       # motor AsyncIOMotorDatabase
        chroma_cols: dict[str, Any],    # {"episodic": col, "semantic": col, "procedural": col}
        budget: ContextBudget | None = None,
    ): ...

    async def build_context(
        self,
        session_id: str,
        current_task: str,
        system_prompt: str,
    ) -> AssembledContext:
        """
        Assemble the full prompt for one agent step.
        Enforces hard sub-budget per slot.
        Raises AssertionError if total_tokens > effective_budget (programming error).
        """
        ...

    async def hybrid_retrieve(
        self,
        query: str,
        collections: list[str] | None = None,   # default: ["semantic", "episodic"]
        top_k_first_stage: int = 50,
        final_k: int = 8,
        token_budget: int = 2_800,
    ) -> list[dict]:
        """
        BM25 (rank_bm25) + Chroma dense → RRF (k=60) → FlagReranker cross-encoder.
        Returns list of {"id": str, "text": str, "score": float} in descending score order.
        Total text of results fits within token_budget.
        """
        ...

    async def consolidation_worker(
        self,
        llm_extract,    # async (core_text: str) -> list[str]
        llm_decide,     # async (candidate: str, similar: list[dict]) -> Literal["ADD","UPDATE","DELETE","NOOP"]
    ) -> None:
        """
        Background coroutine. Reads from the "consolidate" Redis Stream consumer group.
        Extracts semantic facts, reconciles with archival, trims core memory.
        Never called on the agent hot path.
        """
        ...
```

### 4.3 MongoDB document shapes (Contract C, authoritative)

**`sessions` collection**
```json
{
  "_id": "ObjectId",
  "session_id": "uuid-string",
  "created_at": "ISODate",
  "updated_at": "ISODate",
  "status": "active | completed | failed",
  "goal": "string",
  "goal_tree": {
    "id": "root",
    "description": "string",
    "status": "pending | in_progress | completed | failed",
    "children": []
  }
}
```

**`messages` collection**
```json
{
  "_id": "ObjectId",
  "session_id": "uuid-string",
  "sequence": 1,
  "role": "system | user | assistant | tool",
  "content": "string | null",
  "tool_calls": [
    {"id": "call_abc123", "type": "function", "function": {"name": "...", "arguments": "{...}"}}
  ],
  "tool_call_id": "call_abc123",
  "created_at": "ISODate",
  "token_count": 142
}
```

**`outbox` collection** — written atomically alongside `messages` in one Motor session
```json
{
  "_id": "ObjectId",
  "message_id": "ObjectId — references messages._id",
  "session_id": "uuid-string",
  "operation": "upsert | delete",
  "projected": false,
  "created_at": "ISODate"
}
```

**`meta` collection** — singleton documents for worker state
```json
{"_id": "outbox_token", "token": "<MongoDB change stream resume token BSON doc>"}
```

### 4.4 Chroma point shape (Contract D)

All collections use `all-MiniLM-L6-v2` per Contract D. The embedding model stored in
collection metadata must match the embedder used at write time.

```python
# episodic
await col.upsert(
    ids=[str(message["_id"])],          # MongoDB _id — idempotency key
    documents=[message["content"]],
    metadatas=[{
        "session_id": session_id,
        "role": role,
        "created_at": created_at_iso,
        "token_count": token_count,
        "embed_model": "all-MiniLM-L6-v2",
    }]
)

# semantic
await col.upsert(
    ids=[sha1_hex],                     # sha1(session_id + ":" + text)
    documents=[fact_text],
    metadatas=[{
        "session_id": session_id,
        "source_message_id": str(message_id),
        "created_at": created_at_iso,
        "embed_model": "all-MiniLM-L6-v2",
    }]
)

# procedural
await col.upsert(
    ids=[skill_id],
    documents=[skill_description],
    metadatas=[{
        "skill_name": skill_name,
        "success": True,
        "created_at": created_at_iso,
        "embed_model": "all-MiniLM-L6-v2",
    }]
)
```

### 4.5 Redis Stream message format (Contract E, authoritative)

**Task stream key:** `lm:tasks`  
**Consumer group:** `skill-workers`  
**Consumer name:** `worker-{hostname}-{pid}`

```python
# Enqueue (orchestrator → Redis)
await redis.xadd("lm:tasks", {
    "task_id": str(uuid.uuid4()),
    "skill_name": "repo_map",
    "input_json": json.dumps({"path": "/workspace"}),
    "session_id": session_id,
    "correlation_id": tool_call_id,
    "created_at": datetime.utcnow().isoformat(),
})

# Result stream key: lm:results:{session_id}
await redis.xadd(f"lm:results:{session_id}", {
    "task_id": task_id,
    "correlation_id": correlation_id,
    "status": "success | error",
    "output_json": json.dumps(result),
    "completed_at": datetime.utcnow().isoformat(),
})
await redis.expire(f"lm:results:{session_id}", 3600)  # 1-hour TTL
```

**Working memory keys** (Redis, not Streams):

| Key pattern | Type | TTL |
|-------------|------|-----|
| `lm:session:{session_id}:context` | String (JSON) | 30 min |
| `lm:session:{session_id}:state` | String (JSON) | 1 hour |
| `lm:session:{session_id}:tokens` | String (int) | 1 hour |
| `lm:skill:registry` | Hash | none |
| `core:{session_id}` | String | none (explicit delete) |
| `summary:{session_id}` | String | none (explicit delete) |

---

## 5. Implementation Steps

Implement in this exact order. Each step is a discrete task that can be committed
independently. Do not skip ahead — later steps depend on earlier ones being importable.

**Step 1 — `tokenizer.py`: Gemma AutoTokenizer singleton**

Create `services/orchestrator/memory/tokenizer.py`. Load
`transformers.AutoTokenizer.from_pretrained("google/gemma-3-27b-it")` once at module
import time. Expose `token_count(text: str) -> int`. The tokenizer object is thread-safe
for encoding; no lock needed.

**Step 2 — `storage.py` (skeleton): MongoDB connection**

Create `StorageManager.__aenter__` with only the Motor client setup. Verify the connection
with `await client.admin.command("ping")`. Create all MongoDB indexes in `_ensure_indexes()`
— this method must be idempotent (Motor `create_index` is a no-op if the index exists).

**Step 3 — `queue.py`: Redis connection and consumer group bootstrap**

Create the `queue.py` helpers. Build the connection pool from `REDIS_URL`. Implement
`ensure_consumer_group(stream, group)` that swallows the `BUSYGROUP` error. Implement
`enqueue(stream, fields, maxlen)` wrapping `XADD`, and `consume(stream, group, consumer,
count, block_ms)` wrapping `XREADGROUP`, and `reclaim_stale(stream, group, consumer,
min_idle_ms)` wrapping `XAUTOCLAIM`. Wire `StorageManager.__aenter__` to call these.

**Step 4 — `vector.py`: Chroma AsyncHttpClient and collection setup**

Create `vector.py`. Connect with `await chromadb.AsyncHttpClient(host=..., port=...)`.
Create or retrieve all three collections (`episodic`, `semantic`, `procedural`) with
`hnsw:space: cosine` and the embed model in collection metadata. Expose `upsert(col_name,
ids, documents, metadatas, embeddings)` and `query(col_name, query_embeddings, n_results,
where)`. Wire into `StorageManager.__aenter__`.

**Step 5 — `storage.py`: `write_message()` with transactional outbox**

Implement `StorageManager.write_message()`. Open a Motor client session with
`async with await self.mongo.start_session() as session:` and use
`session.start_transaction()`. Inside the transaction, insert the `messages` document and
insert the corresponding `outbox` document in a single atomic operation. Return the
messages `_id`. This is the most critical correctness requirement in the entire module —
if the transaction is not used, you will get outbox entries with no matching message or
vice versa on any crash.

**Step 6 — `storage.py`: `_run_outbox()` background worker**

Implement the change-stream outbox worker. On startup, load the resume token from
`meta.outbox_token`. Open a change stream on the `outbox` collection filtered to
`projected: false` inserts. For each event: upsert the point to Chroma `episodic`
(idempotent — MongoDB `_id` is the Chroma point ID), XADD the consolidation task to
`lm:tasks`, update `outbox.projected = True` in MongoDB, persist the new resume token to
`meta.outbox_token`. Start this as `asyncio.create_task(self._run_outbox())` inside
`__aenter__`. Cancel and await it in `__aexit__`.

**Step 7 — `context.py`: `build_context()` with sub-budget allocation**

Implement `ContextManager.build_context()`. Pull `core:{session_id}` from Redis. Compute
token counts with `token_count()` from `tokenizer.py`. Enforce sub-budgets in order:
system+core (pinned, never trimmed) → RAG (`rag_share` of effective budget) → summary
buffer (`summary_share`) → recent turns (`recent_turns_share`). Assert total does not
exceed `effective_budget`. Return `AssembledContext`.

**Step 8 — `context.py`: `hybrid_retrieve()`**

Implement the two-stage retrieval pipeline. Stage 1: dense query from Chroma, BM25 over
the candidate set via `BM25Okapi(tokenized_docs, k1=1.5, b=0.75)`. Stage 2: RRF fusion
(`score += 1.0 / (60 + rank)` for each ranking). Stage 3: FlagReranker cross-encoder
rerank of the fused top-50 shortlist via `asyncio.to_thread`. Stage 4: pack results into
`token_budget`, highest-scoring first. Return list of `{"id", "text", "score"}`.

**Step 9 — `context.py`: `consolidation_worker()` and `_trim_core_memory()`**

Implement the background consolidation loop. Read from `"consolidate"` consumer group
using `XREADGROUP`. For each session ID: read `core:{session_id}`, call `llm_extract` for
candidate facts, call `llm_decide` (ADD/UPDATE/DELETE/NOOP) comparing against existing
`semantic` memory, execute the operation on Chroma, call `_trim_core_memory()` to evict
non-pinned lines over the 3,000-token cap. Always `XACK` in a `finally` block.

**Step 10 — `__init__.py`: wire public API**

```python
from .storage import StorageManager
from .context import ContextManager, ContextBudget, AssembledContext

__all__ = ["StorageManager", "ContextManager", "ContextBudget", "AssembledContext"]
```

---

## 6. Key Code Patterns

### 6.1 AutoTokenizer singleton (`tokenizer.py`)

```python
# services/orchestrator/memory/tokenizer.py
from __future__ import annotations
from transformers import AutoTokenizer

# Loaded once at module import time. Thread-safe for encode().
# NEVER use tiktoken — it uses GPT BPE and miscounts Gemma tokens by 30%+.
_TOKENIZER = AutoTokenizer.from_pretrained("google/gemma-3-27b-it")


def token_count(text: str) -> int:
    """Count SentencePiece tokens for a string using the Gemma tokenizer.

    This is the ONLY token counting function allowed in the memory module.
    Import this from .tokenizer everywhere; do not instantiate AutoTokenizer elsewhere.
    """
    if not text:
        return 0
    return len(_TOKENIZER.encode(text, add_special_tokens=False))
```

### 6.2 `write_message()` with transactional outbox (`storage.py`)

```python
# services/orchestrator/memory/storage.py
from __future__ import annotations
import time
from datetime import datetime, timezone
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient


async def write_message(
    self,
    session_id: str,
    sequence: int,
    role: str,
    content: str | None,
    token_count: int,
    tool_calls: list[dict] | None = None,
    tool_call_id: str | None = None,
) -> ObjectId:
    """Persist a message and its outbox doc atomically in one Motor session.

    DO NOT write the message and outbox doc in two separate insert_one() calls.
    If the process crashes between them the outbox will be orphaned or missing.
    The Motor session + transaction is the only safe approach.
    """
    msg_id = ObjectId()
    now = datetime.now(timezone.utc)

    message_doc = {
        "_id": msg_id,
        "session_id": session_id,
        "sequence": sequence,
        "role": role,
        "content": content,
        "tool_calls": tool_calls or [],
        "tool_call_id": tool_call_id,
        "created_at": now,
        "token_count": token_count,
    }
    outbox_doc = {
        "message_id": msg_id,
        "session_id": session_id,
        "operation": "upsert",
        "projected": False,
        "created_at": now,
    }

    async with await self.mongo.start_session() as session:
        async with session.start_transaction():
            await self.db.messages.insert_one(message_doc, session=session)
            await self.db.outbox.insert_one(outbox_doc, session=session)
            # Transaction commits automatically when the context manager exits cleanly.
            # On any exception, Motor rolls back both inserts.

    return msg_id
```

### 6.3 `_run_outbox()` coroutine (`storage.py`)

```python
async def _run_outbox(self) -> None:
    """Background worker: tail outbox collection, project to Chroma + Redis.

    CRITICAL: always load the resume token before opening the change stream.
    Starting from "now" on restart silently drops all events that occurred
    during downtime. The resume token persisted in meta.outbox_token is the
    only safeguard against that silent data loss.
    """
    from sentence_transformers import SentenceTransformer
    import asyncio

    embedder = SentenceTransformer("all-MiniLM-L6-v2", device="cuda")

    async def embed(text: str) -> list[float]:
        vecs = await asyncio.to_thread(
            lambda: embedder.encode([text], normalize_embeddings=True).tolist()
        )
        return vecs[0]

    # Load persisted resume token — NEVER skip this.
    tok_doc = await self.db.meta.find_one({"_id": "outbox_token"})
    resume_token = tok_doc["token"] if tok_doc else None

    pipeline = [{"$match": {"operationType": "insert", "fullDocument.projected": False}}]

    async with self.db.outbox.watch(
        pipeline,
        resume_after=resume_token,
        full_document="updateLookup",
    ) as stream:
        async for change in stream:
            outbox_doc = change["fullDocument"]
            outbox_id = outbox_doc["_id"]
            msg_id = outbox_doc["message_id"]
            session_id = outbox_doc["session_id"]

            # Fetch the message from source of truth
            message = await self.db.messages.find_one({"_id": msg_id})
            if message is None:
                # Orphan outbox entry — should not happen but don't crash
                continue

            content = message.get("content") or ""

            # Compute embedding
            vec = await embed(content) if content else []

            if vec:
                # Idempotent upsert: MongoDB _id IS the Chroma point ID
                await self.vectors["episodic"].upsert(
                    ids=[str(msg_id)],
                    embeddings=[vec],
                    documents=[content],
                    metadatas=[{
                        "session_id": session_id,
                        "role": message.get("role", ""),
                        "created_at": message["created_at"].isoformat(),
                        "token_count": message.get("token_count", 0),
                        "embed_model": "all-MiniLM-L6-v2",
                    }],
                )

            # Enqueue consolidation task (XADD to lm:tasks)
            import uuid, json
            from datetime import datetime, timezone
            await self.redis.xadd("lm:tasks", {
                "task_id": str(uuid.uuid4()),
                "skill_name": "consolidate",
                "input_json": json.dumps({"message_id": str(msg_id)}),
                "session_id": session_id,
                "correlation_id": str(outbox_id),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }, maxlen=10_000, approximate=True)

            # Mark projected in MongoDB
            await self.db.outbox.update_one(
                {"_id": outbox_id},
                {"$set": {"projected": True}},
            )

            # Persist resume token AFTER successful processing.
            # If this write fails, the event will be re-delivered on restart.
            # The Chroma upsert is idempotent so the re-delivery is safe.
            await self.db.meta.update_one(
                {"_id": "outbox_token"},
                {"$set": {"token": change["_id"]}},
                upsert=True,
            )
```

### 6.4 `build_context()` with sub-budget allocation (`context.py`)

```python
from __future__ import annotations
from dataclasses import dataclass
from .tokenizer import token_count


@dataclass
class ContextBudget:
    max_tokens: int = 8192
    completion_reserve: int = 700
    system_core_share: float = 0.25
    recent_turns_share: float = 0.30
    rag_share: float = 0.35
    summary_share: float = 0.10

    @property
    def effective_budget(self) -> int:
        return self.max_tokens - self.completion_reserve

    def slot(self, share: float) -> int:
        return int(self.effective_budget * share)


async def build_context(
    self,
    session_id: str,
    current_task: str,
    system_prompt: str,
) -> AssembledContext:
    b = self.budget

    # 1. Pinned slots — system prompt + core memory. These are NEVER trimmed.
    #    If they exceed their share, the other slots shrink, not these.
    core = await self.redis.get(f"core:{session_id}") or ""
    pinned_tokens = token_count(system_prompt) + token_count(core)
    remaining = b.effective_budget - pinned_tokens

    if remaining <= 0:
        # System + core alone exceed budget. Log a warning; do not crash.
        # The orchestrator should not let core memory grow past 3000 tokens.
        remaining = 0

    # 2. Hybrid RAG — pull from remaining budget
    rag_budget = min(b.slot(b.rag_share), remaining)
    rag_chunks = await self.hybrid_retrieve(current_task, token_budget=rag_budget)
    rag_text = "\n\n".join(c["text"] for c in rag_chunks)
    remaining -= token_count(rag_text)
    remaining = max(remaining, 0)

    # 3. Summary buffer
    summary_budget = min(b.slot(b.summary_share), remaining)
    summary = await self.redis.get(f"summary:{session_id}") or ""
    summary = self._trim_to_budget(summary, summary_budget)
    remaining -= token_count(summary)
    remaining = max(remaining, 0)

    # 4. Recent turns (sliding window, newest retained when trimmed)
    recent_budget = min(b.slot(b.recent_turns_share), remaining)
    recent = await self._recent_turns(session_id, recent_budget)

    ctx = AssembledContext(
        system_prompt=system_prompt,
        core_memory=core,
        recent_turns=recent,
        retrieved_context=rag_text,
        summary_buffer=summary,
    )
    ctx.total_tokens = token_count(ctx.as_prompt())
    assert ctx.total_tokens <= b.effective_budget, (
        f"Budget overflow: {ctx.total_tokens} > {b.effective_budget}. "
        f"This is a programming error in build_context()."
    )
    return ctx


def _trim_to_budget(self, text: str, budget: int) -> str:
    """Trim text from the front (oldest first) until it fits the token budget."""
    if token_count(text) <= budget:
        return text
    lines = text.splitlines()
    while lines and token_count("\n".join(lines)) > budget:
        lines.pop(0)  # drop oldest line
    return "\n".join(lines)


async def _recent_turns(self, session_id: str, budget: int) -> str:
    """Load recent turns from MongoDB in descending order, trim to budget."""
    cursor = (
        self.db.messages
        .find({"session_id": session_id}, {"role": 1, "content": 1, "sequence": 1})
        .sort("sequence", -1)
        .limit(50)
    )
    turns = [doc async for doc in cursor]
    turns.reverse()  # restore chronological order
    lines = [
        f"{t['role'].upper()}: {t['content'] or ''}"
        for t in turns
    ]
    return self._trim_to_budget("\n".join(lines), budget)
```

### 6.5 `hybrid_retrieve()` — BM25 + Chroma → RRF → FlagReranker (`context.py`)

```python
async def hybrid_retrieve(
    self,
    query: str,
    collections: list[str] | None = None,
    top_k_first_stage: int = 50,
    final_k: int = 8,
    token_budget: int = 2_800,
) -> list[dict]:
    from rank_bm25 import BM25Okapi
    from FlagEmbedding import FlagReranker
    import asyncio
    from sentence_transformers import SentenceTransformer

    cols = collections or ["semantic", "episodic"]

    # Embed the query for dense retrieval
    embedder = SentenceTransformer("all-MiniLM-L6-v2", device="cuda")
    query_vec = await asyncio.to_thread(
        lambda: embedder.encode([query], normalize_embeddings=True).tolist()[0]
    )

    all_docs: dict[str, str] = {}       # chroma_id -> text
    dense_rankings: list[list[str]] = []
    bm25_rankings: list[list[str]] = []

    for col_name in cols:
        col = self.chroma[col_name]

        # Dense retrieval from Chroma
        res = await col.query(
            query_embeddings=[query_vec],
            n_results=min(top_k_first_stage, 100),
            include=["documents", "metadatas"],
        )
        ids: list[str] = res["ids"][0]
        docs: list[str] = res["documents"][0]
        for cid, doc in zip(ids, docs):
            all_docs[cid] = doc
        dense_rankings.append(ids)

        # BM25 over the same candidate set (local, no separate index needed)
        if ids:
            tokenized = [d.lower().split() for d in docs]
            bm25 = BM25Okapi(tokenized, k1=1.5, b=0.75)
            scores = bm25.get_scores(query.lower().split())
            ranked_ids = [
                ids[i] for i in sorted(range(len(scores)), key=lambda x: -scores[x])
            ]
            bm25_rankings.append(ranked_ids)

    # RRF fusion (k=60): score(d) = sum(1 / (60 + rank_r(d)))
    rrf_scores: dict[str, float] = {}
    for ranking in dense_rankings + bm25_rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            rrf_scores[doc_id] = rrf_scores.get(doc_id, 0.0) + 1.0 / (60 + rank)

    shortlist_ids = sorted(rrf_scores, key=lambda x: -rrf_scores[x])[:top_k_first_stage]
    shortlist_docs = [all_docs[cid] for cid in shortlist_ids if cid in all_docs]

    if not shortlist_docs:
        return []

    # Cross-encoder rerank (FlagReranker, fp16 for A6000 speed)
    reranker = FlagReranker("BAAI/bge-reranker-v2-m3", use_fp16=True)
    pairs = [[query, doc] for doc in shortlist_docs]
    rerank_scores: list[float] = await asyncio.to_thread(reranker.compute_score, pairs)

    ranked = sorted(
        zip(rerank_scores, shortlist_ids, shortlist_docs),
        key=lambda x: -x[0],
    )

    # Pack into token_budget, highest-score first
    results: list[dict] = []
    used = 0
    for score, cid, text in ranked[:final_k]:
        t = token_count(text)
        if used + t > token_budget:
            break
        used += t
        results.append({"id": cid, "text": text, "score": float(score)})

    return results
```

### 6.6 `enqueue_task()` via XADD (`queue.py`)

```python
# services/orchestrator/memory/queue.py
from __future__ import annotations
import uuid
import json
from datetime import datetime, timezone
import redis.asyncio as aioredis


async def ensure_consumer_group(
    redis: aioredis.Redis,
    stream: str,
    group: str,
) -> None:
    """Create the consumer group idempotently. Safe to call on every startup."""
    try:
        await redis.xgroup_create(stream, group, id="0", mkstream=True)
    except aioredis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def enqueue(
    redis: aioredis.Redis,
    stream: str,
    fields: dict,
    maxlen: int = 10_000,
) -> str:
    """XADD to a Redis Stream. Returns the entry ID string."""
    return await redis.xadd(stream, fields, maxlen=maxlen, approximate=True)


async def consume(
    redis: aioredis.Redis,
    stream: str,
    group: str,
    consumer: str,
    count: int = 10,
    block_ms: int = 5_000,
) -> list[tuple[str, dict]]:
    """XREADGROUP. Returns list of (entry_id, fields) pairs. Returns [] on timeout."""
    entries = await redis.xreadgroup(
        groupname=group,
        consumername=consumer,
        streams={stream: ">"},
        count=count,
        block=block_ms,
    )
    if not entries:
        return []
    result = []
    for _stream_name, messages in entries:
        for entry_id, fields in messages:
            result.append((entry_id, fields))
    return result


async def reclaim_stale(
    redis: aioredis.Redis,
    stream: str,
    group: str,
    consumer: str,
    min_idle_ms: int = 60_000,
) -> list[tuple[str, dict]]:
    """XAUTOCLAIM: reclaim messages idle longer than min_idle_ms."""
    claimed = await redis.xautoclaim(
        stream, group, consumer,
        min_idle_time=min_idle_ms,
        start_id="0-0",
        count=100,
    )
    # claimed is (next_start_id, [(entry_id, fields), ...], [deleted_ids])
    _next_id, messages, _deleted = claimed
    return [(eid, fields) for eid, fields in messages]


# Example: enqueue a task with the full Contract E message format
async def enqueue_task(
    redis: aioredis.Redis,
    skill_name: str,
    input_data: dict,
    session_id: str,
    correlation_id: str,
) -> str:
    task_id = str(uuid.uuid4())
    return await enqueue(redis, "lm:tasks", {
        "task_id": task_id,
        "skill_name": skill_name,
        "input_json": json.dumps(input_data),
        "session_id": session_id,
        "correlation_id": correlation_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
```

---

## 7. Integration Verification

Run these commands after implementing each step. All assume the three containers are
running and `MONGO_URI`, `CHROMA_URL`, `REDIS_URL` are exported.

### 7.1 Write a message and verify it appears in MongoDB

```bash
python - <<'EOF'
import asyncio, os
from services.orchestrator.memory import StorageManager

async def main():
    async with StorageManager.from_env() as sm:
        msg_id = await sm.write_message(
            session_id="test-session-001",
            sequence=1,
            role="user",
            content="hello memory layer",
            token_count=3,
        )
        print(f"Inserted message: {msg_id}")

        # Verify in MongoDB
        doc = await sm.db.messages.find_one({"_id": msg_id})
        assert doc is not None, "message not found in MongoDB"
        assert doc["content"] == "hello memory layer"
        print(f"MongoDB OK — sequence={doc['sequence']}, role={doc['role']}")

        # Verify outbox doc was created in the same transaction
        outbox = await sm.db.outbox.find_one({"message_id": msg_id})
        assert outbox is not None, "outbox doc missing — transaction may not be working"
        assert outbox["projected"] is False, "outbox should start unprocessed"
        print(f"Outbox OK — projected={outbox['projected']}")

asyncio.run(main())
EOF
```

### 7.2 Verify the outbox worker projects to Chroma

```bash
python - <<'EOF'
import asyncio, time
from services.orchestrator.memory import StorageManager

async def main():
    async with StorageManager.from_env() as sm:
        msg_id = await sm.write_message(
            session_id="test-session-002",
            sequence=1,
            role="assistant",
            content="the outbox worker should project this",
            token_count=8,
        )
        print(f"Wrote message {msg_id}, waiting for outbox worker...")
        await asyncio.sleep(3)  # give the change-stream worker time to process

        # Check Chroma
        col = sm.vectors["episodic"]
        result = await col.get(ids=[str(msg_id)], include=["documents"])
        assert result["ids"], f"Chroma point {msg_id} not found — outbox worker may not be running"
        assert result["documents"][0] == "the outbox worker should project this"
        print("Chroma OK — point upserted with correct document text")

        # Check outbox marked projected
        outbox = await sm.db.outbox.find_one({"message_id": msg_id})
        assert outbox["projected"] is True, "outbox not marked projected after Chroma upsert"
        print("Outbox marked projected=True OK")

asyncio.run(main())
EOF
```

### 7.3 Enqueue a task and verify it appears in the Redis stream

```bash
python - <<'EOF'
import asyncio
from services.orchestrator.memory import StorageManager
from services.orchestrator.memory.queue import ensure_consumer_group

async def main():
    async with StorageManager.from_env() as sm:
        await ensure_consumer_group(sm.redis, "lm:tasks", "skill-workers")

        entry_id = await sm.enqueue_task(
            stream="lm:tasks",
            fields={
                "task_id": "test-task-001",
                "skill_name": "repo_map",
                "input_json": '{"path": "/workspace"}',
                "session_id": "test-session-003",
                "correlation_id": "call_test001",
                "created_at": "2026-06-16T00:00:00Z",
            },
        )
        print(f"XADD entry ID: {entry_id}")

        # Read it back
        entries = await sm.redis.xrange("lm:tasks", count=10)
        assert any(eid == entry_id for eid, _ in entries), "entry not found in stream"
        print(f"Redis stream OK — entry {entry_id} confirmed in lm:tasks")

asyncio.run(main())
EOF
```

### 7.4 Call `hybrid_retrieve()` and verify ranked results

```bash
python - <<'EOF'
import asyncio
from services.orchestrator.memory import StorageManager, ContextManager, ContextBudget
from sentence_transformers import SentenceTransformer
import redis.asyncio as aioredis

async def main():
    async with StorageManager.from_env() as sm:
        # Seed one document to retrieve
        msg_id = await sm.write_message(
            session_id="test-session-004",
            sequence=1,
            role="user",
            content="the project uses pnpm for package management",
            token_count=8,
        )
        await asyncio.sleep(3)  # wait for outbox worker

        # Build ContextManager
        cm = ContextManager(
            redis=sm.redis,
            mongo_db=sm.db,
            chroma_cols=sm.vectors,
        )

        results = await cm.hybrid_retrieve(
            query="package manager",
            collections=["episodic"],
            final_k=5,
        )
        print(f"hybrid_retrieve returned {len(results)} results")
        assert results, "expected at least one result"
        assert any("pnpm" in r["text"] for r in results), "pnpm fact not retrieved"
        print(f"Top result score={results[0]['score']:.4f}: {results[0]['text'][:80]}")
        print("hybrid_retrieve OK")

asyncio.run(main())
EOF
```

### 7.5 Token counting sanity check (must use Gemma tokenizer)

```bash
python - <<'EOF'
from services.orchestrator.memory.tokenizer import token_count

# Smoke test: Gemma SentencePiece vs GPT BPE diverge on code tokens.
# If you accidentally installed tiktoken, this will print a different count.
sample = 'async def write_message(session_id: str, role: str) -> ObjectId: ...'
count = token_count(sample)
print(f"token_count({repr(sample)}) = {count}")
assert isinstance(count, int) and count > 0
print("Tokenizer OK — using Gemma SentencePiece AutoTokenizer")
EOF
```

---

## 8. Done Criteria

The memory layer is working when ALL of the following are true. Each criterion is
observable by running the verification commands in Section 7.

1. `StorageManager.from_env()` starts without error when all three containers are running.

2. `write_message()` inserts exactly two documents atomically: one in `messages` and one
   in `outbox`, with `outbox.projected = False`. If either insert fails, both are rolled
   back and no partial state exists in MongoDB.

3. The outbox worker (`_run_outbox()`) is running as a background task and processes each
   new outbox document within 5 seconds of insert. After processing: the Chroma `episodic`
   collection contains a point whose ID matches the MongoDB `messages._id`, and
   `outbox.projected` is `True`.

4. After a simulated restart (kill and restart the orchestrator while outbox documents
   remain unprocessed), all unprocessed outbox documents are projected to Chroma within
   60 seconds of restart. The resume token in `meta.outbox_token` is how the worker knows
   where to restart.

5. `enqueue_task()` returns a valid Redis entry ID (format `<milliseconds>-<sequence>`)
   and the entry is readable via `XRANGE lm:tasks`.

6. `hybrid_retrieve("pnpm package manager")` with a seeded `episodic` document containing
   "pnpm" returns that document in the top 5 results, demonstrating that BM25 catches
   exact token matches that dense embeddings might miss.

7. `build_context()` returns an `AssembledContext` whose `total_tokens` (measured by
   `token_count(ctx.as_prompt())`) is strictly less than `effective_budget` (7,492 for
   the default 8,192 budget). The assertion inside `build_context()` enforces this.

8. `token_count()` in `tokenizer.py` uses `transformers.AutoTokenizer`, not tiktoken.
   Verify by checking `import sys; "tiktoken" not in sys.modules` after importing the
   memory module.

9. All three Chroma collections (`episodic`, `semantic`, `procedural`) exist and have
   `embed_model` in their metadata, confirmed by
   `await client.get_collection("episodic").metadata`.

10. The `consolidation_worker()` runs as a background asyncio task, reads from the
    `"consolidate"` consumer group, and always `XACK`s each entry — even when
    `llm_extract` or `llm_decide` raise an exception.

---

## 9. Common Mistakes

**Mistake 1 — Using tiktoken instead of the Gemma AutoTokenizer**

This is the most critical mistake and the hardest to catch because tiktoken returns
plausible-looking numbers. Gemma uses SentencePiece; tiktoken uses GPT BPE
(`cl100k_base` / `o200k_base`). On code-heavy prompts the difference is 30%+. Using
tiktoken will cause the token budget to silently overflow or underfill, and you will not
see an error — just degraded model behavior or truncated context. Use ONLY:
```python
from services.orchestrator.memory.tokenizer import token_count
```
Never import `tiktoken` anywhere in this module.

**Mistake 2 — Using `chromadb.PersistentClient` or `chromadb.EphemeralClient`**

`EphemeralClient` is RAM-only — all vectors are lost on process exit. `PersistentClient`
holds a file lock that blocks multiple async workers and is not safe for asyncio. The
orchestrator uses asyncio and may restart frequently. Always connect via:
```python
client = await chromadb.AsyncHttpClient(host="chroma", port=8000)
```
Chroma must be running as a standalone server (`chroma run --host 0.0.0.0 --port 8000`).

**Mistake 3 — Using BRPOP instead of Redis Streams**

`LIST + BRPOP` drops tasks permanently if the consumer crashes after the pop but before
completing the work. Use `XREADGROUP` + `XACK`. Unacked entries stay in the Pending
Entries List (PEL) and are recovered by `XAUTOCLAIM`. Never use BLPOP or BRPOP in this
codebase.

**Mistake 4 — Writing message and outbox in two separate `insert_one()` calls**

If the process crashes between the two inserts, you get a message with no outbox entry
(never projected to Chroma) or an outbox entry with no corresponding message (worker
fails to fetch the document). Always use a Motor client session and transaction:
```python
async with await self.mongo.start_session() as session:
    async with session.start_transaction():
        await self.db.messages.insert_one(msg_doc, session=session)
        await self.db.outbox.insert_one(outbox_doc, session=session)
```

**Mistake 5 — Not loading the resume token on outbox worker startup**

Opening the change stream from "now" (`resume_after=None`) silently drops all outbox
events that occurred while the orchestrator was down. The resume token in
`meta.outbox_token` must be loaded before every `watch()` call:
```python
tok_doc = await self.db.meta.find_one({"_id": "outbox_token"})
resume_token = tok_doc["token"] if tok_doc else None
# ...
async with self.db.outbox.watch(pipeline, resume_after=resume_token) as stream:
```

**Mistake 6 — Writing to Chroma before the MongoDB transaction commits**

The outbox worker fires on a change stream event, which MongoDB only emits after the
transaction has committed. Do not try to eagerly write to Chroma inside `write_message()`
itself — that executes before the transaction is durable. The correct flow is:
write_message (transaction commits) → change stream event fires → outbox worker reads
the event → worker writes to Chroma. Never short-circuit this sequence.

**Mistake 7 — Creating a new `AsyncIOMotorClient`, Chroma client, or Redis client per request**

Each client creation opens new TCP connections and does not reuse the pool. At any
meaningful load this exhausts file descriptors and connection limits in seconds.
Create exactly one of each client at orchestrator startup in `StorageManager.__aenter__`
and share it for the lifetime of the process.

**Mistake 8 — Evicting the goal/plan block from core memory (Sisyphus Trap)**

`_trim_core_memory()` must never evict line 0 of `core:{session_id}`. Line 0 is the
pinned current goal. Evicting it causes goal drift mid-session — the agent no longer
knows what it is trying to accomplish and the session must be restarted from scratch.
Only lines 1+ are eligible for eviction when the core memory cap is approached.

**Mistake 9 — Forgetting `XACK` in the consolidation worker**

If `XACK` is not called after processing a task, the entry stays in the PEL indefinitely
and `XAUTOCLAIM` will re-deliver it to every subsequent worker. Put `XACK` in a
`finally` block so it runs even when `llm_extract` or `llm_decide` raise:
```python
for entry_id, fields in messages:
    try:
        await self._consolidate_session(session_id, llm_extract, llm_decide)
    except Exception as exc:
        logger.error(f"consolidation error for {session_id}: {exc}")
    finally:
        await self.redis.xack("consolidate", "consolidation_workers", entry_id)
```

**Mistake 10 — Running `FlagReranker` or `SentenceTransformer` model loads inside `hybrid_retrieve()`**

Loading the model on every call adds 1–5 seconds of startup latency per retrieval.
Both `SentenceTransformer` and `FlagReranker` must be instantiated once (at
`ContextManager.__init__` time or as module-level singletons) and reused across calls.
Model loading is thread-safe once complete; inference is not — wrap inference calls in
`asyncio.to_thread()`.
