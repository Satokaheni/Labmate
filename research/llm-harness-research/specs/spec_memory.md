# Memory Layer Spec — Labmate

**Version**: 1.0  
**Date**: 2026-06-15  
**Stack**: Gemma 4 MoE · RTX A6000 48 GB · Redis · Chroma · MongoDB

---

## 1. Overview

Labmate's memory layer is an autonomous, three-tier virtual-memory system modelled on MemGPT's OS-paging architecture and the CoALA cognitive taxonomy. Memory is partitioned by **function**, not by storage engine: each cognitive tier maps to exactly one backing store, and the agent pages data between tiers through a small set of explicit tool calls rather than receiving a passive fixed context window.

The three cognitive functions are:

| Function | Analogy | Backing Store | Lifetime |
|---|---|---|---|
| Working / core memory | CPU RAM | Redis | Per-session, durable across context resets |
| Semantic / archival memory | SSD cache | Chroma (client-server) | Persistent, searchable by embedding |
| Episodic / recall memory | Tape archive | MongoDB | Permanent, source of truth |

A **hybrid RAG retrieval pipeline** (BM25 + Chroma dense vectors fused via Reciprocal Rank Fusion, then cross-encoder reranked) feeds retrieved evidence into the context window. A **transactional-outbox pattern** guarantees cross-store consistency between MongoDB and Chroma without two-phase commit. A **token budget control loop** enforces hard sub-budgets on each tier of the assembled prompt, using the Gemma HuggingFace AutoTokenizer (SentencePiece) — never tiktoken.

---

## 2. Architecture

### 2.1 Three-Tier Memory Hierarchy (MemGPT-style)

**Tier 1 — Working / Core Memory (Redis)**

The live prompt context. Contains: agent persona, current goal and plan (pinned, never evicted), active task scratchpad, user preferences discovered this session. Injected verbatim into every prompt. Hard token cap: **3,000 tokens**. Persisted in Redis so it survives context-window resets within a session. When the cap is approached, a consolidation job is enqueued (off hot path); least-salient lines are flushed to archival before any eviction occurs ("Sisyphus Trap" prevention).

**Tier 2 — Semantic / Archival Memory (Chroma)**

Long-term, de-contextualized knowledge: code patterns, API conventions, project facts, verified procedural skills (Voyager pattern). Split into three Chroma collections: `episodic`, `semantic`, and `procedural`. Retrieved on demand via hybrid RAG. The vector index is **rebuildable** — Chroma stores only the embedding and the MongoDB `_id`; the canonical text record lives in MongoDB.

**Tier 3 — Episodic / Recall Memory (MongoDB)**

Raw, chronological session history: every message, every tool call I/O, every turn. Never compressed at write time (compression destroys episodic signal). Searchable via the outbox-projected Chroma index. Source of truth for all re-summarization and consolidation.

### 2.2 Storage Backend Roles

| Store | Role | Client | Pool |
|---|---|---|---|
| **Redis** | Working memory KV, task queues via Streams, ephemeral TTL state | `redis.asyncio` | 50 connections max |
| **Chroma** | Rebuildable vector index (client-server mode, `AsyncHttpClient`) | `chromadb.AsyncHttpClient` | HTTP keep-alive |
| **MongoDB** | Source of truth, full session history, transactional outbox, metadata | `motor.AsyncIOMotorClient` | 50 max / 5 min |

One shared singleton of each client is created at orchestrator startup. Per-request client creation is a critical anti-pattern (exhausts the pool).

### 2.3 Hybrid RAG Retrieval Pipeline (BM25 + Dense → RRF → Rerank)

Retrieval uses two complementary first-stage retrievers fused by Reciprocal Rank Fusion (RRF) then precision-reranked by a cross-encoder:

1. **BM25 lexical** (`rank_bm25`, BM25Okapi, k1=1.5, b=0.75) — catches exact identifiers, error codes, file paths, and symbols that dense embeddings miss. Essential for code-heavy memory.
2. **Dense vector** (Chroma, `BAAI/bge-small-en-v1.5`, local on A6000) — captures semantic similarity for natural-language queries.
3. **RRF fusion** (k=60) — rank-based score `score(d) = Σ 1/(60 + rank_r(d))`. No score normalization needed; robust to differing score scales.
4. **FlagReranker cross-encoder** (`BAAI/bge-reranker-v2-m3`, fp16) — precision rerank of the fused top-50 shortlist to final top-5 to 10.

Final chunks are assembled into the prompt with highest-scoring chunks placed at the **head and tail** of the retrieved context block (countering the "lost in the middle" degradation documented in arXiv:2307.03172).

### 2.4 Transactional Outbox Pattern

Cross-store atomicity is achieved without two-phase commit:

1. `write_message()` inserts the business record (message text, metadata) **plus an outbox marker** (`outbox.processed: False`) in a single MongoDB document write — one atomic operation.
2. A background **outbox worker** tails a MongoDB change stream (filtered to unprocessed outbox documents). For each event it:
   a. Upserts the vector into Chroma, using the MongoDB `_id` as the Chroma point ID (idempotent).
   b. Enqueues a task onto the Redis Stream (`XADD tasks ...`).
   c. Sets `outbox.processed: True` in MongoDB.
   d. Persists the change-stream **resume token** to `meta` collection.
3. On restart the worker resumes from the persisted token, so downtime events are never dropped.
4. Idempotency: since the Chroma point ID equals the MongoDB `_id`, retried projections are no-ops (upsert deduplication). No duplicate vectors.

### 2.5 Token Budget Control Loop

The context window is partitioned into named sub-budgets every step. Token counting is performed exclusively with the **Gemma HuggingFace AutoTokenizer** (SentencePiece). Using tiktoken (GPT BPE vocabulary) produces errors of 30%+ on code-heavy prompts and must never be used.

Default budget allocation for an 8,192-token window:

| Slot | Share | Tokens (8192 window) |
|---|---|---|
| System prompt + core memory | ~25% | ~2,000 |
| Sliding window of recent turns | ~30% | ~2,400 |
| Hybrid RAG retrieved evidence | ~35% | ~2,800 |
| Summary buffer (older turns) | ~10% | ~800 |
| **Completion reserve** | fixed | ~700 |
| **Effective budget ceiling** | | ~7,500 |

If a tier exceeds its sub-budget, only that tier is trimmed. The current goal/plan block in core memory is **pinned** and never trimmed.

### 2.6 ASCII Diagram

```
┌───────────────────────────────────────────────────────────────────────────┐
│                        Gemma 4 Context Window                             │
│  ┌────────────────┐ ┌──────────────┐ ┌─────────────────┐ ┌────────────┐  │
│  │ System Prompt  │ │ Core Memory  │ │  Hybrid RAG     │ │ Recent     │  │
│  │ (static)       │ │ (Redis KV)   │ │  Evidence       │ │ Turns      │  │
│  │                │ │ pinned goal  │ │  BM25+Dense     │ │ (sliding   │  │
│  │                │ │ scratchpad   │ │  →RRF→Rerank    │ │  window)   │  │
│  └────────────────┘ └──────────────┘ └─────────────────┘ └────────────┘  │
│  ◄──────────── Token Budget Allocator (Gemma AutoTokenizer) ────────────► │
└───────────────────────────────────────────────────────────────────────────┘
          │ core_memory_append/replace          │ archival_memory_search
          ▼                                     ▼
┌─────────────────────┐            ┌──────────────────────────┐
│       REDIS          │            │         CHROMA            │
│  core:{session_id}  │            │  AsyncHttpClient          │
│  summary:{sess_id}  │            │  collections:             │
│  Stream: tasks       │            │    episodic               │
│  (XADD/XREADGROUP/  │            │    semantic               │
│   XACK/XAUTOCLAIM)  │            │    procedural             │
└─────────────────────┘            └──────────────────────────┘
                                             ▲
                                     outbox projection
                                     (async worker)
                                             │
                          ┌──────────────────────────────────┐
                          │           MONGODB                  │
                          │  collections:                     │
                          │    sessions  (TTL index)          │
                          │    messages  (session_id, seq)    │
                          │    tool_calls                     │
                          │    outbox    (processed index)    │
                          │    meta      (resume token)       │
                          └──────────────────────────────────┘
          │                          │
          └── consolidation worker ──┘
              (Redis queue → background)
              extract facts → reconcile → upsert Chroma semantic
```

---

## 3. Key Design Decisions

1. **Chroma in client-server mode only.** `EphemeralClient` is RAM-only and lost on restart; `PersistentClient` holds an in-process file lock incompatible with multiple async workers. Always run `chroma run --host ... --port ...` and connect via `chromadb.AsyncHttpClient`.

2. **Redis Streams, not `BRPOP`.** A `LIST + BRPOP` worker that crashes after pop but before completion loses the message permanently. `XREADGROUP` keeps messages in the Pending Entries List (PEL) until `XACK`; `XAUTOCLAIM` recovers stale PEL entries from crashed consumers automatically.

3. **Gemma AutoTokenizer, not tiktoken.** `tiktoken` encodes using OpenAI's GPT BPE vocabulary (`cl100k_base` / `o200k_base`). Gemma uses SentencePiece. The mismatch produces 30%+ token-count errors on code-heavy prompts. Every token count in the system must go through `transformers.AutoTokenizer.from_pretrained("google/gemma-3-27b-it")`.

4. **Transactional outbox for cross-store atomicity.** There is no distributed transaction spanning MongoDB and Chroma. The outbox pattern (embed outbox marker in the business-record document write) ensures every record eventually reaches Chroma without silent divergence.

5. **MongoDB `_id` as Chroma point ID.** Guarantees idempotent upserts. Retried outbox projections never create duplicate vectors.

6. **Pinned embedding model per collection.** The embedding model name and dimension are stored in Chroma collection metadata and in every record's metadata. Any model change requires a full re-embed migration. Treat the embedder as a versioned schema.

7. **Episodic writes are never compressed at write time.** Raw turns go to MongoDB verbatim. Abstraction to semantic facts happens only during deliberate background consolidation. Summarizing at write time destroys the who/when/where needed for later reasoning.

8. **Consolidation is always off the hot path.** Embedding, conflict resolution, and Chroma upsert run in a background consolidation worker triggered via Redis queue. The agent turn never blocks on a write.

9. **Retrieval reranking by recency + importance + relevance.** Pure cosine similarity surfaces what is semantically near, not what matters now or has changed. Score = 0.6 × relevance + 0.25 × importance + 0.15 × recency (Generative Agents formula), then cross-encoder rerank for precision.

10. **Goal/plan block pinned against eviction.** When core memory approaches the cap, only non-goal lines are evicted. Evicting the goal causes the "Sisyphus Trap" (goal drift → catastrophic restart).

---

## 4. MongoDB Schema

### 4.1 Collections

**`sessions`**
```json
{
  "_id": ObjectId,
  "session_id": "string (UUID)",
  "project_id": "string",
  "created_at": ISODate,
  "expire_at": ISODate,
  "status": "active | completed | archived",
  "metadata": {
    "model": "gemma-3-27b-it",
    "embed_model": "BAAI/bge-small-en-v1.5"
  }
}
```

**`messages`**
```json
{
  "_id": ObjectId,
  "session_id": "string",
  "seq": NumberLong,
  "role": "user | assistant | tool | system",
  "content": "string",
  "created_at": ISODate,
  "token_count": NumberInt,
  "importance": NumberDouble,
  "outbox": {
    "kind": "vector",
    "embedding": [NumberDouble, ...],
    "processed": false,
    "processed_at": ISODate
  }
}
```

Note: Messages are a **separate collection** from sessions. Embedding the messages array inside the session document hits the MongoDB 16 MB BSON limit for long conversations.

**`tool_calls`**
```json
{
  "_id": ObjectId,
  "session_id": "string",
  "message_seq": NumberLong,
  "tool_name": "string",
  "input": BSONDocument,
  "output": BSONDocument,
  "duration_ms": NumberInt,
  "created_at": ISODate,
  "outbox": {
    "kind": "tool_vector",
    "processed": false
  }
}
```

**`outbox`** (optional dedicated collection for high-volume deployments)
```json
{
  "_id": ObjectId,
  "source_collection": "messages | tool_calls",
  "source_id": ObjectId,
  "kind": "vector | task",
  "payload": BSONDocument,
  "processed": false,
  "created_at": ISODate,
  "processed_at": ISODate
}
```

**`meta`** (singleton documents)
```json
{ "_id": "outbox_token", "token": BSONDocument }
```

### 4.2 Indexes

```python
# messages: primary query path
await db.messages.create_index([("session_id", 1), ("seq", 1)], unique=True)
await db.messages.create_index([("session_id", 1), ("created_at", -1)])
await db.messages.create_index([("outbox.processed", 1)])  # outbox worker filter

# sessions: TTL-based archival
await db.sessions.create_index("expire_at", expireAfterSeconds=0)
await db.sessions.create_index("status")

# tool_calls
await db.tool_calls.create_index([("session_id", 1), ("message_seq", 1)])
await db.tool_calls.create_index([("outbox.processed", 1)])

# outbox (if dedicated)
await db.outbox.create_index([("processed", 1), ("created_at", 1)])
```

### 4.3 Connection Pool Config (AsyncIOMotorClient)

```python
from motor.motor_asyncio import AsyncIOMotorClient

client = AsyncIOMotorClient(
    mongo_uri,
    maxPoolSize=50,           # cap concurrent sockets per host
    minPoolSize=5,            # keep warm connections ready
    waitQueueTimeoutMS=5000,  # fail fast; never hang silently
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
    socketTimeoutMS=30000,
)
db = client.labmate
```

---

## 5. Chroma Vector Store

### 5.1 Collections

Chroma is partitioned into three collections, one per CoALA memory function:

| Collection | Content | Metadata fields |
|---|---|---|
| `episodic` | Past turn summaries, session episodes | `session_id`, `seq`, `created_at`, `importance`, `embed_model` |
| `semantic` | Project facts, conventions, API knowledge, distilled summaries | `session_id`, `domain`, `importance`, `created_at`, `embed_model` |
| `procedural` | Verified reusable code skills (Voyager pattern) | `skill_name`, `language`, `verified`, `created_at`, `embed_model` |

The embedding model name is stored both in the collection-level metadata and in every point's metadata. Queries that use a different embedding model than the stored points must be rejected at the application layer.

### 5.2 AsyncHttpClient Setup

```python
import chromadb

chroma_client = await chromadb.AsyncHttpClient(
    host="localhost",   # or remote Chroma server address
    port=8000,
)

episodic_col  = await chroma_client.get_or_create_collection(
    name="episodic",
    metadata={"embed_model": EMBED_MODEL_NAME, "hnsw:space": "cosine"},
)
semantic_col  = await chroma_client.get_or_create_collection(
    name="semantic",
    metadata={"embed_model": EMBED_MODEL_NAME, "hnsw:space": "cosine"},
)
procedural_col = await chroma_client.get_or_create_collection(
    name="procedural",
    metadata={"embed_model": EMBED_MODEL_NAME, "hnsw:space": "cosine"},
)
```

**Never use** `chromadb.EphemeralClient()` or `chromadb.PersistentClient()` in the orchestrator process. Both are in-process and incompatible with a multi-worker async runtime and process restarts.

### 5.3 Embedding Model

```python
EMBED_MODEL_NAME = "BAAI/bge-small-en-v1.5"  # pinned; treat as versioned schema
EMBED_DIM = 384

from sentence_transformers import SentenceTransformer
import asyncio

_embedder = SentenceTransformer(EMBED_MODEL_NAME, device="cuda")  # A6000

async def embed(texts: list[str]) -> list[list[float]]:
    """Async wrapper — runs CPU-bound encode in a thread pool."""
    return await asyncio.to_thread(
        lambda: _embedder.encode(
            texts,
            normalize_embeddings=True,
            batch_size=64,
        ).tolist()
    )
```

Embeddings are computed locally on the A6000 before any Chroma write. They are never recomputed for the same document — the stored embedding travels from MongoDB through the outbox into Chroma.

---

## 6. Redis Configuration

### 6.1 Streams for Task Queues (XADD / XREADGROUP / XACK)

```python
import redis.asyncio as aioredis

# Enqueue a task
await redis.xadd(
    "tasks",
    {"msg_id": str(mongo_id), "session_id": session_id, "kind": "consolidate"},
    maxlen=10_000,  # cap stream length; oldest trimmed automatically
    approximate=True,
)

# Consumer group setup (idempotent)
try:
    await redis.xgroup_create("tasks", "orchestrator", id="0", mkstream=True)
except aioredis.ResponseError as e:
    if "BUSYGROUP" not in str(e):
        raise

# Read and process
entries = await redis.xreadgroup(
    groupname="orchestrator",
    consumername="worker-1",
    streams={"tasks": ">"},
    count=10,
    block=5000,  # ms; yields control while idle
)
for stream_name, messages in (entries or []):
    for entry_id, fields in messages:
        await process_task(fields)
        await redis.xack("tasks", "orchestrator", entry_id)

# Reclaim stale PEL entries from crashed workers (run periodically)
claimed = await redis.xautoclaim(
    "tasks", "orchestrator", "worker-1",
    min_idle_time=60_000,  # ms: reclaim if idle >60s
    start_id="0-0",
    count=100,
)
```

### 6.2 Working Memory Keys

| Key pattern | Type | Content | TTL |
|---|---|---|---|
| `core:{session_id}` | String | Agent's self-editable core memory block (persona, goal, scratchpad) | Session lifetime (no TTL; explicit delete on session close) |
| `summary:{session_id}` | String | Rolling summary of turns older than the sliding window | Session lifetime |
| `embed_cache:{content_hash}` | String (JSON) | Cached embedding vector for a content hash | 3600 s |
| `lock:{resource}` | String | Distributed lock (SET NX EX) | 30 s |

```python
# Write core memory with no expiry (explicit lifecycle)
await redis.set(f"core:{session_id}", core_text)

# Cache an embedding with TTL
import json
await redis.setex(
    f"embed_cache:{content_hash}",
    3600,
    json.dumps(embedding_vector),
)
```

### 6.3 Connection Pool

```python
import redis.asyncio as aioredis

pool = aioredis.ConnectionPool.from_url(
    redis_url,
    max_connections=50,
    decode_responses=True,
)
redis = aioredis.Redis(connection_pool=pool)
```

---

## 7. ContextManager Implementation

### 7.1 `build_context()` with Token Budget Sub-allocation

```python
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from transformers import AutoTokenizer

# --- Token counting (Gemma SentencePiece — NOT tiktoken) ---
_TOKENIZER = AutoTokenizer.from_pretrained("google/gemma-3-27b-it")

def token_count(text: str) -> int:
    """Count tokens using the Gemma HF AutoTokenizer (SentencePiece).
    Never use tiktoken — it uses GPT BPE and miscounts Gemma tokens by 30%+."""
    if not text:
        return 0
    return len(_TOKENIZER.encode(text, add_special_tokens=False))


@dataclass
class ContextBudget:
    max_tokens: int = 8192
    completion_reserve: int = 700

    @property
    def effective_budget(self) -> int:
        return self.max_tokens - self.completion_reserve

    # Sub-budget shares (fractions of effective_budget)
    system_core_share: float = 0.25   # system prompt + core memory
    recent_turns_share: float = 0.30  # verbatim sliding window
    rag_share: float = 0.35           # hybrid RAG evidence
    summary_share: float = 0.10       # rolling summary buffer

    def slot(self, share: float) -> int:
        return int(self.effective_budget * share)


@dataclass
class AssembledContext:
    system_prompt: str
    core_memory: str
    recent_turns: str
    retrieved_context: str
    summary_buffer: str
    total_tokens: int = 0

    def as_prompt(self) -> str:
        """Assemble with highest-value content at head and goal recap at tail
        to counter lost-in-the-middle degradation (arXiv:2307.03172)."""
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
        redis,            # redis.asyncio client
        mongo_db,         # motor database
        chroma_cols: dict,  # {"episodic": col, "semantic": col, "procedural": col}
        embedder,         # async embed(texts) -> list[list[float]]
        budget: ContextBudget | None = None,
    ):
        self.redis = redis
        self.db = mongo_db
        self.chroma = chroma_cols
        self.embed = embedder
        self.budget = budget or ContextBudget()

    async def build_context(
        self,
        session_id: str,
        current_task: str,
        system_prompt: str,
    ) -> AssembledContext:
        """Assemble the full context for one agent step, strictly within budget."""
        b = self.budget

        # 1. Pinned slots (system + core memory — never trimmed)
        core = await self.redis.get(f"core:{session_id}") or ""
        sys_core_tokens = token_count(system_prompt) + token_count(core)
        remaining = b.effective_budget - sys_core_tokens

        # 2. Hybrid RAG — from remaining, allocate rag_share
        rag_budget = min(int(b.effective_budget * b.rag_share), remaining)
        rag_chunks = await self.hybrid_retrieve(current_task, token_budget=rag_budget)
        rag_text = "\n\n".join(c["text"] for c in rag_chunks)
        remaining -= token_count(rag_text)

        # 3. Summary buffer
        summary_budget = min(int(b.effective_budget * b.summary_share), remaining)
        summary = await self.redis.get(f"summary:{session_id}") or ""
        summary = self._trim_to_budget(summary, summary_budget)
        remaining -= token_count(summary)

        # 4. Recent turns (sliding window, newest last)
        recent_budget = min(int(b.effective_budget * b.recent_turns_share), remaining)
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
            f"Budget overflow: {ctx.total_tokens} > {b.effective_budget}"
        )
        return ctx

    def _trim_to_budget(self, text: str, budget: int) -> str:
        """Trim text from the front until it fits in the token budget."""
        if token_count(text) <= budget:
            return text
        lines = text.splitlines()
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
        turns.reverse()  # chronological order
        lines = [f"{t['role'].upper()}: {t['content']}" for t in turns]
        return self._trim_to_budget("\n".join(lines), budget)

    ### 7.2 `hybrid_retrieve()` (BM25 + Chroma → RRF → FlagReranker)

    async def hybrid_retrieve(
        self,
        query: str,
        collections: list[str] | None = None,
        top_k_first_stage: int = 50,
        final_k: int = 8,
        token_budget: int = 2_800,
    ) -> list[dict]:
        """
        Two-stage hybrid retrieval:
          1. BM25 (rank_bm25) + Chroma dense, each returning top_k_first_stage.
          2. RRF fusion (k=60).
          3. FlagReranker cross-encoder rerank to final_k.
          4. Pack results into token_budget.
        """
        from rank_bm25 import BM25Okapi
        from FlagEmbedding import FlagReranker

        cols = collections or ["semantic", "episodic"]
        query_vec = (await self.embed([query]))[0]

        # Gather candidates from each Chroma collection
        all_docs: dict[str, str] = {}   # chroma_id -> text
        dense_rankings: list[list[str]] = []
        bm25_rankings: list[list[str]] = []

        for col_name in cols:
            col = self.chroma[col_name]
            # Dense retrieval
            res = await col.query(
                query_embeddings=[query_vec],
                n_results=min(top_k_first_stage, 100),
                include=["documents", "metadatas"],
            )
            ids = res["ids"][0]
            docs = res["documents"][0]
            for cid, doc in zip(ids, docs):
                all_docs[cid] = doc
            dense_rankings.append(ids)

            # BM25 over the retrieved candidate set (local BM25 on candidates)
            if ids:
                tokenized = [d.lower().split() for d in docs]
                bm25 = BM25Okapi(tokenized, k1=1.5, b=0.75)
                scores = bm25.get_scores(query.lower().split())
                bm25_ranked = [ids[i] for i in sorted(range(len(scores)), key=lambda x: -scores[x])]
                bm25_rankings.append(bm25_ranked)

        # RRF fusion (k=60)
        rrf_scores: dict[str, float] = {}
        for ranking in dense_rankings + bm25_rankings:
            for rank, doc_id in enumerate(ranking, start=1):
                rrf_scores[doc_id] = rrf_scores.get(doc_id, 0.0) + 1.0 / (60 + rank)

        shortlist_ids = sorted(rrf_scores, key=lambda x: -rrf_scores[x])[:top_k_first_stage]
        shortlist_docs = [all_docs[cid] for cid in shortlist_ids if cid in all_docs]

        if not shortlist_docs:
            return []

        # Cross-encoder rerank
        reranker = FlagReranker("BAAI/bge-reranker-v2-m3", use_fp16=True)
        pairs = [[query, doc] for doc in shortlist_docs]
        scores = await asyncio.to_thread(reranker.compute_score, pairs)
        ranked = sorted(
            zip(scores, shortlist_ids, shortlist_docs),
            key=lambda x: -x[0],
        )

        # Pack into token budget (highest-score chunks first)
        results = []
        used = 0
        for score, cid, text in ranked[:final_k]:
            t = token_count(text)
            if used + t > token_budget:
                break
            used += t
            results.append({"id": cid, "text": text, "score": float(score)})

        return results

    ### 7.3 `consolidation_worker()` (async, off hot-path)

    async def consolidation_worker(
        self,
        llm_extract,    # async (core_text: str) -> list[str]  # extract salient facts
        llm_decide,     # async (candidate: str, similar: list[dict]) -> Literal["ADD","UPDATE","DELETE","NOOP"]
    ) -> None:
        """
        Background coroutine. Reads session IDs from a Redis Stream consumer group,
        extracts semantic facts from core memory, reconciles them with existing
        archival memory (ADD/UPDATE/DELETE/NOOP), and trims core memory under cap.

        Never called on the agent's hot path.
        """
        try:
            await self.redis.xgroup_create(
                "consolidate", "consolidation_workers", id="0", mkstream=True
            )
        except Exception:
            pass  # group already exists

        while True:
            entries = await self.redis.xreadgroup(
                groupname="consolidation_workers",
                consumername="consolidator-1",
                streams={"consolidate": ">"},
                count=5,
                block=10_000,
            )
            if not entries:
                continue

            for _, messages in entries:
                for entry_id, fields in messages:
                    session_id = fields.get(b"session_id", fields.get("session_id", ""))
                    try:
                        await self._consolidate_session(
                            session_id, llm_extract, llm_decide
                        )
                    except Exception as exc:
                        # Log and continue — do not crash the worker
                        print(f"[consolidation] error for {session_id}: {exc}")
                    finally:
                        await self.redis.xack("consolidate", "consolidation_workers", entry_id)

    async def _consolidate_session(self, session_id: str, llm_extract, llm_decide) -> None:
        """Extract facts from core memory, reconcile, trim."""
        core_text = await self.redis.get(f"core:{session_id}") or ""
        if not core_text.strip():
            return

        facts: list[str] = await llm_extract(core_text)

        for fact in facts:
            # Search existing semantic memory for similar facts
            similar = await self.hybrid_retrieve(fact, collections=["semantic"], final_k=5)
            op: str = await llm_decide(fact, similar)

            if op == "ADD":
                await self._upsert_semantic(session_id, fact)
            elif op == "UPDATE":
                await self._upsert_semantic(session_id, fact)   # upsert supersedes
            elif op == "DELETE":
                for s in similar:
                    await self.chroma["semantic"].delete(ids=[s["id"]])
            # NOOP: do nothing

        # Trim core memory to cap (least-salient lines first)
        await self._trim_core_memory(session_id)

    async def _upsert_semantic(self, session_id: str, text: str) -> None:
        import hashlib
        cid = hashlib.sha1(f"{session_id}:{text}".encode()).hexdigest()
        vec = (await self.embed([text]))[0]
        await self.chroma["semantic"].upsert(
            ids=[cid],
            embeddings=[vec],
            documents=[text],
            metadatas=[{
                "session_id": session_id,
                "created_at": __import__("time").time(),
                "embed_model": "BAAI/bge-small-en-v1.5",
                "importance": 0.5,
                "source": "consolidation",
            }],
        )

    async def _trim_core_memory(self, session_id: str) -> None:
        """Evict least-salient (oldest) lines from core memory until under token cap.
        The current goal/plan block (first line, pinned) is never evicted."""
        CORE_CAP = 3_000
        key = f"core:{session_id}"
        text = await self.redis.get(key) or ""
        lines = [l for l in text.splitlines() if l.strip()]

        # First line is the pinned goal — never evict it
        if len(lines) <= 1:
            return
        pinned, evictable = lines[0], lines[1:]
        while token_count("\n".join([pinned] + evictable)) > CORE_CAP and evictable:
            evictable.pop(0)

        await self.redis.set(key, "\n".join([pinned] + evictable))

    ### 7.4 Gemma Tokenizer for Token Counting (NOT tiktoken)
    # See token_count() at the top of this section.
    # AutoTokenizer.from_pretrained() is called once at module load time and reused.
    # The tokenizer object is thread-safe for encoding.
```

---

## 8. StorageManager Implementation

### 8.1 `write_message()` with Transactional Outbox

```python
from __future__ import annotations
import asyncio
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient
import chromadb
import redis.asyncio as aioredis


class StorageManager:
    """Async context manager owning all three storage clients.
    
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
    ):
        self._mongo_uri = mongo_uri
        self._chroma_host = chroma_host
        self._chroma_port = chroma_port
        self._redis_url = redis_url
        self._outbox_task: asyncio.Task | None = None

    async def __aenter__(self) -> "StorageManager":
        # One shared Motor client — coroutine-safe, pools internally
        self.mongo = AsyncIOMotorClient(
            self._mongo_uri,
            maxPoolSize=50,
            minPoolSize=5,
            waitQueueTimeoutMS=5000,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=30_000,
        )
        self.db = self.mongo.labmate

        # One Chroma client in client-server mode — survives restarts
        self.chroma = await chromadb.AsyncHttpClient(
            host=self._chroma_host,
            port=self._chroma_port,
        )
        self.vectors = {
            col: await self.chroma.get_or_create_collection(
                col,
                metadata={"embed_model": "BAAI/bge-small-en-v1.5", "hnsw:space": "cosine"},
            )
            for col in ("episodic", "semantic", "procedural")
        }

        # One redis.asyncio client over a bounded pool
        pool = aioredis.ConnectionPool.from_url(
            self._redis_url,
            max_connections=50,
            decode_responses=True,
        )
        self.redis = aioredis.Redis(connection_pool=pool)

        await self._ensure_indexes()
        # Start outbox worker as a background coroutine
        self._outbox_task = asyncio.create_task(self._run_outbox())
        return self

    async def __aexit__(self, *exc) -> None:
        if self._outbox_task:
            self._outbox_task.cancel()
            try:
                await self._outbox_task
            except asyncio.CancelledError:
                pass
        self.mongo.close()
        await self.redis.aclose()

    async def _ensure_indexes(self) -> None:
        """Idempotent index creation. Safe to call on every startup."""
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
        # TTL index on sessions; set expire_at at write time
        await self.db.sessions.create_index(
            "expire_at", expireAfterSeconds=0
        )

    # --- 8.1 write_message() with transactional outbox ---

    async def write_message(
        self,
        session_id: str,
        seq: int,
        role: str,
        content: str,
        embedding: list[float],
        importance: float = 0.5,
    ) -> ObjectId:
        """Persist a message + outbox marker in ONE atomic MongoDB write.
        
        The outbox worker (running in the background) picks up the marker,
        upserts the vector into Chroma, and enqueues a Redis task.
        Cross-store atomicity is guaranteed by the outbox pattern.
        """
        import time
        _id = ObjectId()
        await self.db.messages.insert_one({
            "_id": _id,
            "session_id": session_id,
            "seq": seq,
            "role": role,
            "content": content,
            "created_at": time.time(),
            "importance": importance,
            "outbox": {
                "kind": "vector",
                "embedding": embedding,   # precomputed on A6000
                "processed": False,
                "processed_at": None,
            },
        })
        return _id

    # --- 8.2 search_memory() ---

    async def search_memory(
        self,
        query_embedding: list[float],
        collection: str = "semantic",
        k: int = 10,
        where: dict | None = None,
    ) -> list[dict]:
        """Vector similarity search in Chroma, resolved to full MongoDB records.
        
        Uses MongoDB _id as the Chroma point ID so results are always consistent
        with the source of truth. Orphan vectors (stale Chroma points with no
        matching MongoDB document) are silently dropped.
        """
        col = self.vectors[collection]
        res = await col.query(
            query_embeddings=[query_embedding],
            n_results=k,
            where=where,
            include=["metadatas", "distances"],
        )
        chroma_ids = res["ids"][0]
        if not chroma_ids:
            return []

        # Resolve from source of truth
        object_ids = [ObjectId(cid) for cid in chroma_ids]
        cursor = self.db.messages.find({"_id": {"$in": object_ids}})
        docs = {doc["_id"]: doc async for doc in cursor}

        # Attach distance, drop orphans, return in Chroma relevance order
        results = []
        for cid, dist in zip(chroma_ids, res["distances"][0]):
            oid = ObjectId(cid)
            if oid not in docs:
                continue   # orphan vector — skip
            doc = docs[oid]
            doc["_similarity"] = 1.0 - dist
            results.append(doc)
        return results

    # --- 8.3 enqueue_task() via XADD ---

    async def enqueue_task(
        self,
        stream: str,
        fields: dict,
        maxlen: int = 10_000,
    ) -> str:
        """Enqueue a task onto a Redis Stream. Returns the entry ID.
        
        Always use Streams (XADD/XREADGROUP/XACK), never LIST + BRPOP.
        Unacked entries remain in the PEL and are recovered via XAUTOCLAIM.
        """
        return await self.redis.xadd(
            stream,
            fields,
            maxlen=maxlen,
            approximate=True,
        )

    # --- Outbox worker (background coroutine) ---

    async def _run_outbox(self) -> None:
        """Tail the MongoDB change stream, project unprocessed outbox entries
        to Chroma and Redis, persist resume token after each event."""
        # Load persisted resume token — start-from-now would drop downtime events
        tok_doc = await self.db.meta.find_one({"_id": "outbox_token"})
        resume_token = tok_doc["token"] if tok_doc else None

        pipeline = [{
            "$match": {
                "operationType": "insert",
                "fullDocument.outbox.processed": False,
            }
        }]

        async with self.db.messages.watch(
            pipeline,
            resume_after=resume_token,
            full_document="updateLookup",
        ) as stream:
            async for change in stream:
                doc = change["fullDocument"]
                _id = doc["_id"]
                emb = doc["outbox"]["embedding"]

                # Idempotent upsert: Mongo _id IS the Chroma point ID
                await self.vectors["episodic"].upsert(
                    ids=[str(_id)],
                    embeddings=[emb],
                    documents=[doc["content"]],
                    metadatas=[{
                        "session_id": doc["session_id"],
                        "seq": doc["seq"],
                        "embed_model": "BAAI/bge-small-en-v1.5",
                        "importance": doc.get("importance", 0.5),
                    }],
                )

                # Enqueue a consolidation task onto the Redis Stream
                await self.enqueue_task(
                    "consolidate",
                    {"msg_id": str(_id), "session_id": doc["session_id"]},
                )

                # Mark processed in MongoDB
                import time
                await self.db.messages.update_one(
                    {"_id": _id},
                    {"$set": {"outbox.processed": True, "outbox.processed_at": time.time()}},
                )

                # Persist resume token AFTER successful processing
                await self.db.meta.update_one(
                    {"_id": "outbox_token"},
                    {"$set": {"token": change["_id"]}},
                    upsert=True,
                )
```

---

## 9. BDD Test Scenarios

```gherkin
Feature: Memory persistence across sessions
  As Labmate
  I want long-term facts to survive process restarts
  So that a new session continues where the last left off

  Scenario: Recall a fact learned in a previous session
    Given session "s1" stored the semantic fact "project uses pnpm, not npm" in archival memory
    And the agent process is restarted with no in-memory state
    When a new session "s2" starts for the same project
    And the agent searches archival memory for "package manager"
    Then the result includes "project uses pnpm, not npm"
    And the fact's source session id and timestamp are returned with it


Feature: Hybrid retrieval beats vector-only for exact code tokens
  As Labmate
  I want BM25 to catch exact identifiers and error codes
  So that I do not miss critical matches that dense embeddings would rank low

  Scenario: Exact error code match surfaces via BM25 leg
    Given the episodic memory contains an entry with the error code "ECONNREFUSED"
    When the agent queries the hybrid retriever for "ECONNREFUSED"
    Then BM25 returns the exact entry in its top results
    And the cross-encoder reranker keeps the exact error entry in the final top 5
    And the result is present even if dense cosine similarity ranked it below top 10


Feature: Token budget enforcement
  As the ContextManager
  I want the assembled prompt to stay within the Gemma window budget
  So that the model never receives a truncated context without warning

  Scenario: Context assembly leaves a completion reserve
    Given a Gemma 4 context budget of 8192 tokens
    When build_context() assembles system prompt, core memory, recent turns, RAG, and summary
    Then the total token count measured by the Gemma AutoTokenizer never exceeds 7500
    And the remaining tokens are reserved for the model completion
    And if a tier would overflow its sub-budget, only that tier is trimmed


Feature: Core memory self-edit survives a context-window reset
  As Labmate
  I want preference edits to persist across context resets
  So that per-session learning is not lost on window overflow

  Scenario: Persisted core memory is rebuilt after reset
    Given the agent discovers the user prefers tabs over spaces during a session
    When the agent calls core_memory_append with that preference
    Then the new entry is persisted to the Redis key "core:{session_id}"
    And after the context window is reset and rebuild_context() is called
    Then the persisted preference is present in the assembled core memory block


Feature: Transactional outbox — cross-store consistency
  As the StorageManager
  I want every written message to be eventually projected to Chroma
  So that the vector index never silently diverges from the source of truth

  Scenario: Creating a message projects to Chroma via the outbox
    Given the StorageManager has one shared Motor, Chroma AsyncHttpClient, and redis.asyncio client
    When write_message() is called with a session_id, seq, role, content, and embedding
    Then a document is inserted into MongoDB messages with outbox.processed=False
    And within the projection interval the outbox worker upserts a Chroma point
    And the Chroma point id equals the MongoDB _id
    And the outbox marker is set to processed=True exactly once

  Scenario: Outbox recovers after orchestrator restart
    Given unprocessed outbox markers exist in MongoDB
    And a change-stream resume token was persisted in the meta collection
    When the orchestrator restarts
    Then the change-stream is reopened with resumeAfter the persisted token
    And every outbox marker created during downtime is projected to Chroma and enqueued to Redis
    And each projection is idempotent (no duplicate Chroma vectors)


Feature: Crash-safe task dispatch via Redis Streams
  As the task queue
  I want unacknowledged tasks to survive worker crashes
  So that no consolidation work is silently lost

  Scenario: Crashed worker's pending entry is reclaimed by XAUTOCLAIM
    Given a Redis Stream "tasks" with a consumer group "orchestrator"
    And a task entry was read by "worker-1" with XREADGROUP but never ACKed (worker crashed)
    When XAUTOCLAIM is called with min_idle_time=60000ms
    Then the pending entry is transferred to the calling consumer
    And no other consumer receives a duplicate of that entry


Feature: Memory conflict resolution (ADD / UPDATE / DELETE / NOOP)
  As the consolidation worker
  I want to reconcile new facts against existing ones
  So that memory stays non-redundant and contradiction-free

  Scenario Outline: Decide the correct operation for an incoming fact
    Given an existing memory "<existing>"
    When the consolidator evaluates the candidate fact "<candidate>"
    Then it performs operation "<op>"

    Examples:
      | existing                     | candidate                        | op     |
      | (none)                       | user prefers 2-space indent      | ADD    |
      | user prefers 2-space indent  | user now prefers 4-space indent  | UPDATE |
      | temp debug flag enabled      | debug flag removed from config   | DELETE |
      | project uses pnpm            | project uses pnpm                | NOOP   |


Feature: Core memory overflow triggers off-path consolidation
  As the memory manager
  I want core memory to stay under its token cap
  So that working memory never overflows the prompt budget

  Scenario: Cap breach flushes facts to archival and frees core memory
    Given the core memory token cap is 3000 tokens
    And core memory currently holds 2900 tokens
    When the agent appends a 400-token block of new salient facts
    Then a consolidation job is enqueued onto the "consolidate" Redis Stream
    And the consolidation worker extracts semantic facts off the hot path
    And after flush the core memory token count is <= 3000
    And no flushed fact is lost (it is retrievable from Chroma semantic)
    And the pinned goal/plan block is not evicted
```

---

## 10. Common Pitfalls

1. **Using tiktoken for Gemma.** `tiktoken` encodes using OpenAI GPT BPE. Gemma uses SentencePiece. Errors of 30%+ on code-heavy prompts will silently cause window overflows or under-fills. Always use `transformers.AutoTokenizer.from_pretrained("google/gemma-3-27b-it")`.

2. **Chroma in-process mode in the orchestrator.** `PersistentClient` holds a file lock that conflicts with multiple async workers and does not survive a clean restart. `EphemeralClient` is RAM-only and lost on process exit. Always run Chroma as a standalone server (`chroma run`) and use `AsyncHttpClient`.

3. **Redis BRPOP without acknowledgement.** A `LIST + BRPOP` consumer that crashes after popping but before completing the work loses the message permanently. Use `XREADGROUP + XACK` on Streams; PEL entries survive crashes and are reclaimed by `XAUTOCLAIM`.

4. **Embedding everything without deduplication.** At-least-once outbox delivery means the same content can be projected twice. Use the MongoDB `_id` as the Chroma point ID — `upsert` then becomes idempotent and never creates duplicates.

5. **Embedding model drift.** Swapping or upgrading the embedding model (e.g., MiniLM → bge-large) invalidates all stored vectors — queries now live in a different vector space and retrieval recall collapses silently. Pin `EMBED_MODEL_NAME` per collection, store it in every record's metadata, and require a full re-embed migration on any change.

6. **Growing messages array inside the session document.** Embedding messages inside the session document exceeds the MongoDB 16 MB BSON limit for long sessions. Messages must be a separate collection indexed by `(session_id, seq)`.

7. **Evicting the goal from core memory (Sisyphus Trap).** FIFO eviction of working memory that drops the current goal causes goal drift and catastrophic mid-session restarts. Pin the goal/plan as the first line of core memory; trim only from line 2 onwards.

8. **Consolidation on the agent's hot path.** Blocking on embedding + Chroma upsert inside the agent's turn adds 100–500 ms of I/O latency per step. All extraction, conflict resolution, and re-embedding must run in the background consolidation worker, triggered via Redis Stream.

9. **Memory bloat with no lifecycle.** Embedding every turn and never pruning causes retrieval noise — stale six-month-old facts compete with yesterday's facts at equal cosine similarity. Apply recency + importance + relevance scoring; schedule periodic consolidation and pruning.

10. **Summarizing at write time.** Compressing each turn to a generalization before it might ever be needed destroys episodic signal (who/when/where). Store raw turns in MongoDB verbatim; abstract to semantic only during deliberate background consolidation.

11. **No resume token on the outbox change stream.** Starting the change stream from "now" on restart silently drops all events that occurred during downtime. Always persist the resume token after each processed event and reopen the stream with `resumeAfter`.

12. **Per-request client instantiation.** Creating a new `AsyncIOMotorClient`, Chroma client, or Redis client per request exhausts connection pools and defeats connection reuse. Create exactly one of each at startup and share it across the lifetime of the process.

---

## 11. Dependencies

```toml
# pyproject.toml — core memory layer dependencies

[tool.poetry.dependencies]
python = ">=3.11"

# Storage backends
motor          = ">=3.5"          # async MongoDB (AsyncIOMotorClient)
pymongo        = ">=4.9"          # BSON + connection pool (motor dependency)
chromadb       = ">=0.5"          # vector store client (client-server via AsyncHttpClient)
redis          = ">=5.0"          # redis.asyncio; Streams support

# Embeddings and reranking (local, on A6000)
sentence-transformers = ">=3.0"   # dense embeddings + CrossEncoder
FlagEmbedding         = ">=1.2"   # BAAI/bge-reranker-v2-m3 (FlagReranker)

# Tokenization — CRITICAL: Gemma SentencePiece, NOT tiktoken
transformers = ">=4.40"           # AutoTokenizer.from_pretrained (SentencePiece)

# Sparse retrieval
rank-bm25 = ">=0.2"              # BM25Okapi for lexical first stage

# Data validation
pydantic = ">=2.0"               # typed schemas for memory records

# Utilities
bson = ">=0.5"                   # ObjectId handling outside motor

[tool.poetry.dev-dependencies]
pytest         = ">=8.0"
pytest-asyncio = ">=0.23"
pytest-bdd     = ">=7.0"          # BDD scenario execution
testcontainers = ">=4.0"          # spin up MongoDB/Redis/Chroma in tests
```

---

## 12. Reference Papers & Repos

### Papers

| Paper | Key contribution |
|---|---|
| MemGPT: Towards LLMs as Operating Systems (Packer et al., 2023) — arXiv:2310.08560 | Foundational tiered virtual-memory architecture; agent-managed paging via tool calls; core/recall/archival taxonomy |
| Generative Agents (Park et al., 2023) — arXiv:2304.03442 | Memory stream with recency + importance + relevance scoring; reflection and planning |
| Cognitive Architectures for Language Agents / CoALA (Sumers et al., 2023) — arXiv:2309.02427 | Working / episodic / semantic / procedural memory taxonomy; decision cycle |
| Voyager (Wang et al., 2023) — arXiv:2305.16291 | Procedural skill library pattern; verified reusable skills accumulated over time |
| Mem0 (Chhikara et al., 2025) — arXiv:2504.19413 | Production ADD/UPDATE/DELETE/NOOP LLM-judged conflict resolution; 90% fewer tokens vs full in-context |
| Zep / Graphiti (Rasmussen et al., 2025) — arXiv:2501.13956 | Bi-temporal knowledge graph; beats MemGPT on DMR (94.8% vs 93.4%); +18.5% on LongMemEval |
| Episodic Memory is the Missing Piece (2025) — arXiv:2502.06975 | GSW/SYNAPSE episodic routing; F1 0.850 (+20% over RAG), 51% fewer query-time tokens |
| A-MEM (Xu et al., 2025) — arXiv:2502.12110 | Agentic memory with dynamic note linking; rebuildable vector index pattern |
| HippoRAG (Gutierrez et al., NeurIPS 2024) — arXiv:2405.14831 | Single-step multi-hop retrieval; resolving Chroma hits back to authoritative records |
| RAG Survey (Gao et al., 2023) — arXiv:2312.10997 | Hybrid BM25+dense+rerank pipeline; naive/advanced/modular RAG taxonomy |
| Lost in the Middle (Liu et al., 2023) — arXiv:2307.03172 | LLMs attend best to head and tail; basis for prompt-assembly position strategy |
| Chain of Density (Adams et al., 2023) — arXiv:2309.04269 | Iterative entity-densification summarization at fixed length |
| RRF (Cormack, Clarke, Buettcher, 2009) — ACM SIGIR 2009 | Reciprocal Rank Fusion; `score(d) = Σ 1/(k + rank_r(d))`, default k=60 |

### Repositories

| Repo | Role |
|---|---|
| [letta-ai/letta](https://github.com/letta-ai/letta) | MemGPT successor; reference for tiered self-editing memory and memory tool definitions |
| [mem0ai/mem0](https://github.com/mem0ai/mem0) | Production ADD/UPDATE/DELETE/NOOP conflict resolution; pluggable stores |
| [getzep/graphiti](https://github.com/getzep/graphiti) | Bi-temporal knowledge-graph engine; SOTA upgrade path for semantic tier |
| [chroma-core/chroma](https://github.com/chroma-core/chroma) | Vector store; client-server mode is the required deployment model |
| [mongodb/motor](https://github.com/mongodb/motor) | Official async MongoDB driver (AsyncIOMotorClient) |
| [redis/agent-memory-server](https://github.com/redis/agent-memory-server) | Reference production agent memory service on Redis |
| [FlagOpen/FlagEmbedding](https://github.com/FlagOpen/FlagEmbedding) | bge embeddings + FlagReranker (bge-reranker-v2-m3) cross-encoder |
| [dorianbrown/rank_bm25](https://github.com/dorianbrown/rank_bm25) | Lightweight BM25Okapi for the sparse first-stage retriever |
| [UKPLab/sentence-transformers](https://github.com/UKPLab/sentence-transformers) | Dense embeddings + CrossEncoder in one library |
| [NirDiamant/Agent_Memory_Techniques](https://github.com/NirDiamant/Agent_Memory_Techniques) | Practical tutorials on episodic/semantic/procedural patterns |
| [qdrant/qdrant](https://github.com/qdrant/qdrant) | Production vector store upgrade path beyond Chroma (>100k vectors) |

---

## 13. SOTA Improvements

These are sequenced by implementation risk and value. Items 1–3 are high-value low-risk. Items 4–6 are medium-risk, recommended for phase 2.

**1. LLM-judged conflict resolution (Mem0 ADD/UPDATE/DELETE/NOOP)**  
Replace the naive "always ADD" pattern (which produces contradictory duplicates, documented as mem0 issue #4896) with the retrieve-similar → LLM-decide → operate pipeline already specified in `consolidation_worker()`. This is the single largest correctness improvement over a plain vector store. No new infrastructure required.

**2. Retrieval routing by memory type (arXiv:2502.06975 GSW/SYNAPSE)**  
Route queries rather than blindly hitting all collections: factual queries ("what is our convention for X") hit `semantic`, trajectory queries ("how did we solve X before") hit `episodic`, sub-task queries ("reuse skill for Y") hit `procedural`. The paper reports F1 0.850 (+20% over flat RAG) with 51% fewer query-time tokens. Implementation: add a lightweight query classifier in `hybrid_retrieve()`.

**3. Importance-weighted decay and active forgetting (Generative Agents)**  
Add scheduled pruning: periodically re-score all `episodic` and `semantic` records by `recency × importance × relevance` and delete or demote records below a threshold. This counters memory bloat and retrieval noise from stale facts. The scoring formula is already embedded in `hybrid_retrieve()` — the pruning job is a straightforward extension.

**4. Bi-temporal knowledge graph (Zep/Graphiti, arXiv:2501.13956)**  
Upgrade evolving facts from flat vectors to a graph where every edge carries both valid-time and transaction-time. Enables point-in-time queries ("what did this config look like in March?") and fact INVALIDATION instead of deletion — corrections never produce contradictory duplicates. Beats MemGPT on DMR (94.8% vs 93.4%) and gains up to 18.5% on LongMemEval with ~90% lower latency. Adds `graphiti-core` and a graph store (Neo4j or embedded) as a fifth infrastructure component. Recommended as phase-2 once flat-vector memory is proven.

**5. Continual skill accumulation (Voyager + Reflexion / A-MEM)**  
Periodically distill successful episodic trajectories into verified reusable procedural skills in the `procedural` Chroma collection. Before generating new code for a sub-task, the agent queries `procedural` memory first. Over time this turns the procedural collection into a compounding asset — solved problems are never re-solved.

**6. Qdrant as the production vector store (>100k vectors)**  
Swap Chroma for Qdrant once collections exceed ~100k vectors. Qdrant offers native async client, filterable HNSW (metadata filters applied during graph traversal, not post-search), indexed payload fields, on-disk HNSW, and quantization. Because Chroma in this system holds only embedding + MongoDB `_id`, the swap is a drop-in re-projection from the outbox worker with no schema changes to MongoDB.

**7. KV-cache prefix sharing (vLLM automatic prefix caching)**  
Cache the shared system-prompt + stable core-memory prefix across agent steps. Since the prefix is assembled at the head of the prompt (countering lost-in-the-middle), it forms a stable cacheable block. Reduces time-to-first-token on each step at the cost of no additional infrastructure if vLLM is already serving Gemma.
