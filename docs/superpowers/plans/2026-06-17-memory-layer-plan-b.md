# Memory Layer Plan B — Hybrid RAG + Consolidation Worker

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the memory layer by implementing the hybrid RAG retrieval pipeline (BM25 + Chroma dense → RRF → FlagReranker) and the consolidation worker (Redis Stream → LLM-judged ADD/UPDATE/DELETE/NOOP → Chroma semantic upsert + core memory trim). Wires the `rag_text = ""` stub in `build_context()` to real retrieval.

**Architecture:** Two new modules (`embedder.py`, `reranker.py`) provide lazy GPU-resident singletons. `hybrid_retrieve()` and `consolidation_worker()` are added to `ContextManager`. Both LLM callables (`llm_extract`, `llm_decide`) are injected dependencies — this keeps Plan B testable without a running vLLM server. The orchestrator will wire the real callables when built.

**Tech Stack:** sentence-transformers 3+ (BAAI/bge-small-en-v1.5), FlagEmbedding 1.2+ (BAAI/bge-reranker-v2-m3), rank-bm25 0.2+, asyncio.to_thread (CPU-bound GPU work), Redis embed_cache TTL

**Prerequisite:** Plan A complete — `StorageManager`, `ContextManager`, `token_count()` all passing. The `rag_text = ""` stub is in `context_manager.py:build_context()`.

---

## File Map

| File | Status | Responsibility |
|---|---|---|
| `services/memory/embedder.py` | **New** | `embed()` — async SentenceTransformer wrapper with Redis TTL cache |
| `services/memory/reranker.py` | **New** | `rerank()` — lazy FlagReranker singleton, async via `asyncio.to_thread` |
| `services/memory/context_manager.py` | **Modify** | Add `hybrid_retrieve()`, `consolidation_worker()`, `_consolidate_session()`, `_upsert_semantic()`, `_trim_core_memory()`; wire RAG stub |
| `services/memory/requirements.txt` | **Modify** | Add `FlagEmbedding>=1.2` |
| `services/memory/tests/test_embedder.py` | **New** | Unit tests for embed() + cache |
| `services/memory/tests/test_reranker.py` | **New** | Unit tests for rerank() |
| `services/memory/tests/test_hybrid_retrieve.py` | **New** | Unit tests for hybrid_retrieve() |
| `services/memory/tests/test_consolidation.py` | **New** | Unit tests for consolidation_worker() + helpers |

---

## Task 1: `embedder.py` — SentenceTransformer with Redis Cache

**Files:**
- Create: `services/memory/embedder.py`
- Create: `services/memory/tests/test_embedder.py`
- Modify: `services/memory/requirements.txt` — add `FlagEmbedding>=1.2`

The embedder is a lazy module-level singleton: `_MODEL = None` at import time, loaded on first call. This avoids GPU model download at import time (matches tokenizer pattern). Embedding is CPU-bound; it must run in `asyncio.to_thread`. Results are cached in Redis by SHA-256 of the text, with 3600s TTL, to avoid re-embedding identical content.

- [ ] **Step 1: Add `FlagEmbedding>=1.2` to requirements.txt**

Append to `/Users/zachstallbohm/Work/gemma/services/memory/requirements.txt`:
```
FlagEmbedding>=1.2
```

Install:
```bash
pip install FlagEmbedding>=1.2
```

- [ ] **Step 2: Create `services/memory/tests/test_embedder.py`**

```python
import asyncio
import json
import hashlib
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_redis_mock():
    r = AsyncMock()
    r.get = AsyncMock(return_value=None)
    r.setex = AsyncMock()
    return r


@pytest.mark.asyncio
async def test_embed_returns_vectors():
    mock_model = MagicMock()
    mock_model.encode.return_value = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]

    with patch("services.memory.embedder._MODEL", mock_model):
        from services.memory.embedder import embed
        result = await embed(["hello", "world"])

    assert len(result) == 2
    assert result[0] == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_embed_calls_encode_with_normalize():
    mock_model = MagicMock()
    mock_model.encode.return_value = [[0.1, 0.2]]

    with patch("services.memory.embedder._MODEL", mock_model):
        from services.memory.embedder import embed
        await embed(["test"])

    mock_model.encode.assert_called_once_with(
        ["test"],
        normalize_embeddings=True,
        batch_size=64,
    )


@pytest.mark.asyncio
async def test_embed_uses_redis_cache_on_hit():
    mock_model = MagicMock()
    mock_model.encode.return_value = [[0.9, 0.8, 0.7]]
    redis = _make_redis_mock()
    cached_vec = [0.1, 0.2, 0.3]
    redis.get = AsyncMock(return_value=json.dumps(cached_vec))

    with patch("services.memory.embedder._MODEL", mock_model):
        from services.memory.embedder import embed
        result = await embed(["hello"], redis=redis)

    # Cache hit — model.encode must NOT be called
    mock_model.encode.assert_not_called()
    assert result[0] == cached_vec


@pytest.mark.asyncio
async def test_embed_writes_to_cache_on_miss():
    mock_model = MagicMock()
    mock_model.encode.return_value = [[0.1, 0.2, 0.3]]
    redis = _make_redis_mock()

    with patch("services.memory.embedder._MODEL", mock_model):
        from services.memory.embedder import embed
        await embed(["hello"], redis=redis)

    # Cache miss — setex must be called with the vector
    redis.setex.assert_called_once()
    call_args = redis.setex.call_args[0]
    assert call_args[1] == 3600
    assert json.loads(call_args[2]) == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_embed_cache_key_is_sha256_of_text():
    mock_model = MagicMock()
    mock_model.encode.return_value = [[0.1]]
    redis = _make_redis_mock()

    with patch("services.memory.embedder._MODEL", mock_model):
        from services.memory.embedder import embed
        await embed(["hello"], redis=redis)

    expected_hash = hashlib.sha256("hello".encode()).hexdigest()
    expected_key = f"embed_cache:{expected_hash}"
    # get was called with this key
    redis.get.assert_called_once_with(expected_key)
```

- [ ] **Step 3: Run tests to confirm FAIL**

```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest services/memory/tests/test_embedder.py -v 2>&1 | tail -8
```

Expected: ImportError.

- [ ] **Step 4: Create `services/memory/embedder.py`**

```python
from __future__ import annotations

import asyncio
import hashlib
import json
from functools import lru_cache

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384
_CACHE_TTL = 3600

# Lazy singleton — not loaded at import time; tests can patch _MODEL freely.
_MODEL = None


@lru_cache(maxsize=1)
def _load_model():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(EMBED_MODEL, device="cuda")


async def embed(
    texts: list[str],
    redis=None,
) -> list[list[float]]:
    """Embed a batch of texts using BAAI/bge-small-en-v1.5.

    Results are cached in Redis by SHA-256(text) with a 3600s TTL to avoid
    re-embedding identical content. Pass redis=None to skip caching.
    Encoding is CPU-bound; runs in asyncio.to_thread to avoid blocking the
    event loop.
    """
    results: list[list[float] | None] = [None] * len(texts)
    uncached_indices: list[int] = []
    uncached_texts: list[str] = []

    if redis is not None:
        for i, text in enumerate(texts):
            key = f"embed_cache:{hashlib.sha256(text.encode()).hexdigest()}"
            cached = await redis.get(key)
            if cached is not None:
                results[i] = json.loads(cached)
            else:
                uncached_indices.append(i)
                uncached_texts.append(text)
    else:
        uncached_indices = list(range(len(texts)))
        uncached_texts = texts

    if uncached_texts:
        model = _MODEL if _MODEL is not None else _load_model()
        vectors = await asyncio.to_thread(
            lambda: model.encode(
                uncached_texts,
                normalize_embeddings=True,
                batch_size=64,
            ).tolist()
        )
        for i, (idx, text) in enumerate(zip(uncached_indices, uncached_texts)):
            results[idx] = vectors[i]
            if redis is not None:
                key = f"embed_cache:{hashlib.sha256(text.encode()).hexdigest()}"
                await redis.setex(key, _CACHE_TTL, json.dumps(vectors[i]))

    return results  # type: ignore[return-value]
```

- [ ] **Step 5: Run tests — all 5 should pass**

```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest services/memory/tests/test_embedder.py -v
```

Expected: 5 passed.

- [ ] **Step 6: Run full suite to confirm no regressions**

```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest services/memory/tests/ -v
```

Expected: 20 tests pass (15 existing + 5 new).

---

## Task 2: `reranker.py` — Lazy FlagReranker Singleton

**Files:**
- Create: `services/memory/reranker.py`
- Create: `services/memory/tests/test_reranker.py`

FlagReranker is a cross-encoder — it takes `(query, document)` pairs and returns a relevance score per pair. Instantiation downloads `BAAI/bge-reranker-v2-m3` (~570 MB) and loads it onto the GPU. Like the tokenizer, it is a lazy singleton (`_RERANKER = None`) to avoid model download at import time.

- [ ] **Step 1: Create `services/memory/tests/test_reranker.py`**

```python
import asyncio
import pytest
from unittest.mock import MagicMock, patch


@pytest.mark.asyncio
async def test_rerank_returns_scores_per_pair():
    mock_reranker = MagicMock()
    mock_reranker.compute_score.return_value = [0.9, 0.2, 0.7]

    with patch("services.memory.reranker._RERANKER", mock_reranker):
        from services.memory.reranker import rerank
        scores = await rerank("my query", ["doc a", "doc b", "doc c"])

    assert len(scores) == 3
    assert scores[0] == pytest.approx(0.9)
    assert scores[1] == pytest.approx(0.2)
    assert scores[2] == pytest.approx(0.7)


@pytest.mark.asyncio
async def test_rerank_passes_query_doc_pairs():
    mock_reranker = MagicMock()
    mock_reranker.compute_score.return_value = [0.5]

    with patch("services.memory.reranker._RERANKER", mock_reranker):
        from services.memory.reranker import rerank
        await rerank("query", ["doc"])

    call_args = mock_reranker.compute_score.call_args[0][0]
    assert call_args == [["query", "doc"]]


@pytest.mark.asyncio
async def test_rerank_empty_docs_returns_empty():
    mock_reranker = MagicMock()
    mock_reranker.compute_score.return_value = []

    with patch("services.memory.reranker._RERANKER", mock_reranker):
        from services.memory.reranker import rerank
        scores = await rerank("query", [])

    assert scores == []
    mock_reranker.compute_score.assert_not_called()
```

- [ ] **Step 2: Run tests to confirm FAIL**

```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest services/memory/tests/test_reranker.py -v 2>&1 | tail -6
```

- [ ] **Step 3: Create `services/memory/reranker.py`**

```python
from __future__ import annotations

import asyncio
from functools import lru_cache

RERANK_MODEL = "BAAI/bge-reranker-v2-m3"

# Lazy singleton — avoids GPU model download at import time.
# Tests patch _RERANKER before any call is made.
_RERANKER = None


@lru_cache(maxsize=1)
def _load_reranker():
    from FlagEmbedding import FlagReranker
    return FlagReranker(RERANK_MODEL, use_fp16=True)


async def rerank(query: str, docs: list[str]) -> list[float]:
    """Score (query, doc) pairs with the bge-reranker-v2-m3 cross-encoder.

    Returns one float score per doc, in the same order as `docs`.
    Higher score = more relevant. Runs in asyncio.to_thread (GPU-bound).
    """
    if not docs:
        return []
    model = _RERANKER if _RERANKER is not None else _load_reranker()
    pairs = [[query, doc] for doc in docs]
    scores = await asyncio.to_thread(model.compute_score, pairs)
    return list(scores)
```

- [ ] **Step 4: Run tests — all 3 should pass**

```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest services/memory/tests/test_reranker.py -v
```

- [ ] **Step 5: Run full suite**

```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest services/memory/tests/ -v
```

Expected: 23 tests pass.

---

## Task 3: `hybrid_retrieve()` in ContextManager

**Files:**
- Modify: `services/memory/context_manager.py` — add `hybrid_retrieve()` method
- Create: `services/memory/tests/test_hybrid_retrieve.py`

`hybrid_retrieve()` is a method on `ContextManager`. It takes a query string and returns a list of `{"id", "text", "score"}` dicts packed into a token budget. Internally:

1. Dense: Chroma `col.query()` for each collection → `(ids, docs)`
2. Sparse: BM25Okapi on the dense candidate set → ranked ids
3. RRF fusion (k=60) over all rankings → shortlist
4. Cross-encoder rerank via `rerank()` → final top-k
5. Pack into token budget (highest score first)

- [ ] **Step 1: Create `services/memory/tests/test_hybrid_retrieve.py`**

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _mock_token_count(text: str) -> int:
    return max(0, len(text) // 4)


def _make_chroma_col_mock(ids, docs):
    col = AsyncMock()
    col.query = AsyncMock(return_value={
        "ids": [ids],
        "documents": [docs],
        "metadatas": [[{}] * len(ids)],
    })
    return col


@pytest.mark.asyncio
async def test_hybrid_retrieve_returns_ranked_results():
    with (
        patch("services.memory.context_manager.token_count", side_effect=_mock_token_count),
        patch("services.memory.context_manager.rerank", new_callable=AsyncMock) as mock_rerank,
    ):
        from services.memory.context_manager import ContextManager

        mock_rerank.return_value = [0.95, 0.60]

        col = _make_chroma_col_mock(
            ids=["id1", "id2"],
            docs=["doc about python", "doc about redis"],
        )
        embed = AsyncMock(return_value=[[0.1, 0.2]])

        cm = ContextManager(
            redis=AsyncMock(),
            mongo_db=MagicMock(),
            chroma_cols={"semantic": col, "episodic": col},
            embedder=embed,
        )

        results = await cm.hybrid_retrieve("python redis", collections=["semantic"])

    assert len(results) >= 1
    assert results[0]["score"] == pytest.approx(0.95)
    assert "text" in results[0]
    assert "id" in results[0]


@pytest.mark.asyncio
async def test_hybrid_retrieve_respects_token_budget():
    with (
        patch("services.memory.context_manager.token_count", side_effect=_mock_token_count),
        patch("services.memory.context_manager.rerank", new_callable=AsyncMock) as mock_rerank,
    ):
        from services.memory.context_manager import ContextManager

        # 3 docs, scores descending
        mock_rerank.return_value = [0.9, 0.7, 0.5]

        # Each doc is 80 chars → 20 tokens at 1/4 rate
        col = _make_chroma_col_mock(
            ids=["a", "b", "c"],
            docs=["x" * 80, "y" * 80, "z" * 80],
        )
        embed = AsyncMock(return_value=[[0.1]])

        cm = ContextManager(
            redis=AsyncMock(),
            mongo_db=MagicMock(),
            chroma_cols={"semantic": col},
            embedder=embed,
        )

        # Budget of 30 tokens: only 1 doc (20 tokens) fits
        results = await cm.hybrid_retrieve("query", collections=["semantic"], token_budget=30)

    assert len(results) == 1


@pytest.mark.asyncio
async def test_hybrid_retrieve_empty_chroma_returns_empty():
    with (
        patch("services.memory.context_manager.token_count", side_effect=_mock_token_count),
        patch("services.memory.context_manager.rerank", new_callable=AsyncMock) as mock_rerank,
    ):
        from services.memory.context_manager import ContextManager

        col = _make_chroma_col_mock(ids=[], docs=[])
        embed = AsyncMock(return_value=[[0.1]])

        cm = ContextManager(
            redis=AsyncMock(),
            mongo_db=MagicMock(),
            chroma_cols={"semantic": col},
            embedder=embed,
        )

        results = await cm.hybrid_retrieve("query", collections=["semantic"])

    assert results == []
    mock_rerank.assert_not_called()


@pytest.mark.asyncio
async def test_hybrid_retrieve_rrf_promotes_docs_in_both_rankings():
    """A doc that appears in both dense and BM25 rankings gets a higher RRF score."""
    with (
        patch("services.memory.context_manager.token_count", side_effect=_mock_token_count),
        patch("services.memory.context_manager.rerank", new_callable=AsyncMock) as mock_rerank,
    ):
        from services.memory.context_manager import ContextManager

        # "id2" contains the exact query term so BM25 ranks it #1
        col = _make_chroma_col_mock(
            ids=["id1", "id2", "id3"],
            docs=["generic content", "exact query term here", "other stuff"],
        )
        embed = AsyncMock(return_value=[[0.1]])
        # Return scores in shortlist order (we just verify shortlist is passed)
        mock_rerank.return_value = [0.9, 0.8, 0.7]

        cm = ContextManager(
            redis=AsyncMock(),
            mongo_db=MagicMock(),
            chroma_cols={"semantic": col},
            embedder=embed,
        )

        results = await cm.hybrid_retrieve("exact query term", collections=["semantic"])

    # Results should be returned — we verify rerank was called with the shortlist
    assert mock_rerank.called
    shortlist_docs = mock_rerank.call_args[0][1]
    assert "exact query term here" in shortlist_docs
```

- [ ] **Step 2: Run tests to confirm FAIL**

```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest services/memory/tests/test_hybrid_retrieve.py -v 2>&1 | tail -8
```

- [ ] **Step 3: Add `hybrid_retrieve()` to `context_manager.py`**

Add this import at the top of `context_manager.py` (after existing imports):

```python
from services.memory.reranker import rerank
from rank_bm25 import BM25Okapi
```

Add this method to `ContextManager` (after `_recent_turns`):

```python
async def hybrid_retrieve(
    self,
    query: str,
    collections: list[str] | None = None,
    top_k_first_stage: int = 50,
    final_k: int = 8,
    token_budget: int = 2_800,
) -> list[dict]:
    """Two-stage hybrid retrieval: BM25 + Chroma dense → RRF (k=60) → rerank.

    1. Dense: Chroma query per collection → candidate ids + docs
    2. Sparse: BM25Okapi on the candidate set → ranked ids
    3. RRF fusion (k=60): rank-based score sum across all rankings
    4. Cross-encoder rerank of fused top-50 → final_k results
    5. Pack into token_budget (highest score first)
    """
    cols = collections or ["semantic", "episodic"]
    query_vec = (await self.embed([query]))[0]

    all_docs: dict[str, str] = {}
    dense_rankings: list[list[str]] = []
    bm25_rankings: list[list[str]] = []

    for col_name in cols:
        if col_name not in self.chroma:
            continue
        col = self.chroma[col_name]
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

        if ids:
            tokenized = [d.lower().split() for d in docs]
            bm25 = BM25Okapi(tokenized, k1=1.5, b=0.75)
            scores = bm25.get_scores(query.lower().split())
            bm25_ranked = [
                ids[i] for i in sorted(range(len(scores)), key=lambda x: -scores[x])
            ]
            bm25_rankings.append(bm25_ranked)

    if not all_docs:
        return []

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
    scores = await rerank(query, shortlist_docs)
    ranked = sorted(
        zip(scores, shortlist_ids, shortlist_docs),
        key=lambda x: -x[0],
    )

    # Pack into token budget (highest score first)
    results = []
    used = 0
    for score, cid, text in ranked[:final_k]:
        t = token_count(text)
        if used + t > token_budget:
            break
        used += t
        results.append({"id": cid, "text": text, "score": float(score)})

    return results
```

- [ ] **Step 4: Run hybrid_retrieve tests — all 4 should pass**

```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest services/memory/tests/test_hybrid_retrieve.py -v
```

- [ ] **Step 5: Run full suite**

```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest services/memory/tests/ -v
```

Expected: 27 tests pass.

---

## Task 4: Wire `hybrid_retrieve()` into `build_context()`

**Files:**
- Modify: `services/memory/context_manager.py` — replace `rag_text = ""` stub

- [ ] **Step 1: Locate and verify the stub exists**

```bash
grep -n "Plan B" /Users/zachstallbohm/Work/gemma/services/memory/context_manager.py
```

Expected: finds `# Plan B: await self.hybrid_retrieve(current_task, rag_budget)`.

- [ ] **Step 2: Replace the stub in `build_context()`**

Find this block in `context_manager.py`:

```python
        # 2. RAG evidence (stub — hybrid_retrieve is Plan B)
        rag_text  = ""  # Plan B: await self.hybrid_retrieve(current_task, token_budget=rag_budget)
        remaining -= token_count(rag_text)
```

Replace with:

```python
        # 2. RAG evidence — hybrid BM25 + dense → RRF → rerank
        rag_budget = min(int(b.effective_budget * b.rag_share), max(0, remaining))
        rag_chunks = await self.hybrid_retrieve(current_task, token_budget=rag_budget)
        rag_text   = "\n\n".join(c["text"] for c in rag_chunks)
        remaining -= token_count(rag_text)
```

- [ ] **Step 3: Update the existing `test_build_context_stays_within_budget` test**

The test in `test_context_manager.py` uses mocked Redis and a real `ContextManager`. Now that `build_context()` calls `hybrid_retrieve()`, which calls `self.embed` and `rerank`, the test must also mock those. 

Open `services/memory/tests/test_context_manager.py` and update `test_build_context_stays_within_budget` to add a `rerank` patch:

```python
@pytest.mark.asyncio
async def test_build_context_stays_within_budget():
    with (
        patch("services.memory.context_manager.token_count", side_effect=_mock_token_count),
        patch("services.memory.context_manager.rerank", new_callable=AsyncMock, return_value=[]),
    ):
        from services.memory.context_manager import ContextManager, ContextBudget

        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=lambda key: "goal: finish MCP bridge" if "core" in key else "old summary")
        db = MagicMock()

        class AsyncDocIter:
            def __init__(self, docs):
                self._docs = iter(docs)
            def __aiter__(self): return self
            async def __anext__(self):
                try:
                    return next(self._docs)
                except StopIteration:
                    raise StopAsyncIteration

        turns = [
            {"role": "user", "content": "hello", "seq": 1},
            {"role": "assistant", "content": "hi there", "seq": 2},
        ]

        mock_cursor = AsyncDocIter(turns)
        mock_sort = MagicMock()
        mock_sort.limit = MagicMock(return_value=mock_cursor)
        mock_find = MagicMock()
        mock_find.sort = MagicMock(return_value=mock_sort)
        db.messages.find = MagicMock(return_value=mock_find)

        embed = AsyncMock(return_value=[[0.1, 0.2]])

        budget = ContextBudget(max_tokens=200, completion_reserve=20)
        cm = ContextManager(
            redis=redis, mongo_db=db,
            chroma_cols={},   # empty — hybrid_retrieve will find no collections
            embedder=embed, budget=budget,
        )

        ctx = await cm.build_context(
            session_id="s1",
            current_task="implement feature X",
            system_prompt="You are Labmate.",
        )

        assert ctx.total_tokens <= budget.effective_budget
        assert "You are Labmate." in ctx.system_prompt
        assert ctx.core_memory


@pytest.mark.asyncio
async def test_build_context_pins_core_memory_even_when_over_budget():
    """Core memory is never trimmed — only summary and recent turns are."""
    with (
        patch("services.memory.context_manager.token_count", side_effect=_mock_token_count),
        patch("services.memory.context_manager.rerank", new_callable=AsyncMock, return_value=[]),
    ):
        from services.memory.context_manager import ContextManager, ContextBudget

        redis = AsyncMock()
        long_core = "GOAL: " + "x" * 1994
        redis.get = AsyncMock(side_effect=lambda key: long_core if "core" in key else "")

        db = MagicMock()

        class EmptyCursor:
            def __aiter__(self): return self
            async def __anext__(self): raise StopAsyncIteration

        empty = EmptyCursor()
        mock_sort = MagicMock()
        mock_sort.limit = MagicMock(return_value=empty)
        mock_find = MagicMock()
        mock_find.sort = MagicMock(return_value=mock_sort)
        db.messages.find = MagicMock(return_value=mock_find)

        embed = AsyncMock(return_value=[[0.1]])
        budget = ContextBudget(max_tokens=700, completion_reserve=100)
        cm = ContextManager(
            redis=redis, mongo_db=db,
            chroma_cols={}, embedder=embed, budget=budget,
        )

        ctx = await cm.build_context("s1", "task", "system")
        assert ctx.core_memory == long_core
```

- [ ] **Step 4: Run full test suite**

```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest services/memory/tests/ -v
```

Expected: 27 tests pass. The two updated `test_context_manager` tests must still pass.

---

## Task 5: Consolidation Worker + Helpers

**Files:**
- Modify: `services/memory/context_manager.py` — add `consolidation_worker()`, `_consolidate_session()`, `_upsert_semantic()`, `_trim_core_memory()`
- Create: `services/memory/tests/test_consolidation.py`

The consolidation worker reads from the Redis Stream `"consolidate"` using `XREADGROUP` + `XACK`. For each message it calls `_consolidate_session()`, which:
1. Reads core memory from Redis
2. Calls `llm_extract(core_text) -> list[str]` to extract salient facts
3. For each fact: calls `hybrid_retrieve()` against `semantic`, then calls `llm_decide(fact, similar) -> "ADD"|"UPDATE"|"DELETE"|"NOOP"`
4. Operates: ADD/UPDATE → `_upsert_semantic()`, DELETE → Chroma delete, NOOP → skip
5. Calls `_trim_core_memory()` to enforce the 3,000-token cap

`_trim_core_memory()`: evicts oldest non-goal lines from core memory. Line 0 (the pinned goal) is never evicted. Trims until `token_count(core_text) <= 3000`.

`_upsert_semantic()`: embeds the fact text → upserts to `chroma["semantic"]`. Chroma ID is `sha1(session_id:text)` for deduplication.

- [ ] **Step 1: Create `services/memory/tests/test_consolidation.py`**

```python
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _mock_token_count(text: str) -> int:
    return max(0, len(text) // 4)


@pytest.mark.asyncio
async def test_consolidation_worker_reads_and_acks_stream():
    with (
        patch("services.memory.context_manager.token_count", side_effect=_mock_token_count),
        patch("services.memory.context_manager.rerank", new_callable=AsyncMock, return_value=[]),
    ):
        from services.memory.context_manager import ContextManager

        redis = AsyncMock()
        # First call returns one entry, second call returns empty (stops loop)
        redis.xreadgroup = AsyncMock(side_effect=[
            [("consolidate", [("1-0", {"session_id": "s1"})])],
            None,  # no more entries → loop idles, we cancel
        ])
        redis.xack = AsyncMock()
        redis.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
        redis.get = AsyncMock(return_value="GOAL: test\nsome fact")

        chroma_col = AsyncMock()
        chroma_col.query = AsyncMock(return_value={"ids": [[]], "documents": [[]], "metadatas": [[]]})
        chroma_col.upsert = AsyncMock()

        embed = AsyncMock(return_value=[[0.1, 0.2]])
        llm_extract = AsyncMock(return_value=["user prefers tabs"])
        llm_decide = AsyncMock(return_value="ADD")

        cm = ContextManager(
            redis=redis,
            mongo_db=MagicMock(),
            chroma_cols={"semantic": chroma_col, "episodic": chroma_col},
            embedder=embed,
        )

        # Run worker as a task; cancel after a short time
        task = asyncio.create_task(
            cm.consolidation_worker(llm_extract, llm_decide)
        )
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    redis.xack.assert_called_with("consolidate", "consolidation_workers", "1-0")


@pytest.mark.asyncio
async def test_consolidation_add_upserts_to_semantic():
    with (
        patch("services.memory.context_manager.token_count", side_effect=_mock_token_count),
        patch("services.memory.context_manager.rerank", new_callable=AsyncMock, return_value=[]),
    ):
        from services.memory.context_manager import ContextManager

        redis = AsyncMock()
        redis.get = AsyncMock(return_value="GOAL: do X\nfact one")
        redis.set = AsyncMock()

        chroma_col = AsyncMock()
        chroma_col.query = AsyncMock(return_value={"ids": [[]], "documents": [[]], "metadatas": [[]]})
        chroma_col.upsert = AsyncMock()

        embed = AsyncMock(return_value=[[0.5, 0.5]])
        llm_extract = AsyncMock(return_value=["user prefers tabs"])
        llm_decide = AsyncMock(return_value="ADD")

        cm = ContextManager(
            redis=redis,
            mongo_db=MagicMock(),
            chroma_cols={"semantic": chroma_col, "episodic": chroma_col},
            embedder=embed,
        )

        await cm._consolidate_session("s1", llm_extract, llm_decide)

    chroma_col.upsert.assert_called_once()
    upsert_kwargs = chroma_col.upsert.call_args.kwargs
    assert upsert_kwargs["documents"] == ["user prefers tabs"]


@pytest.mark.asyncio
async def test_consolidation_delete_removes_from_chroma():
    with (
        patch("services.memory.context_manager.token_count", side_effect=_mock_token_count),
        patch("services.memory.context_manager.rerank", new_callable=AsyncMock, return_value=[0.9]),
    ):
        from services.memory.context_manager import ContextManager

        redis = AsyncMock()
        redis.get = AsyncMock(return_value="GOAL: do X\nstale fact")
        redis.set = AsyncMock()

        chroma_col = AsyncMock()
        chroma_col.query = AsyncMock(return_value={
            "ids": [["existing-id"]],
            "documents": [["stale fact"]],
            "metadatas": [[{}]],
        })
        chroma_col.delete = AsyncMock()
        chroma_col.upsert = AsyncMock()

        embed = AsyncMock(return_value=[[0.5]])
        llm_extract = AsyncMock(return_value=["stale fact"])
        llm_decide = AsyncMock(return_value="DELETE")

        cm = ContextManager(
            redis=redis,
            mongo_db=MagicMock(),
            chroma_cols={"semantic": chroma_col, "episodic": chroma_col},
            embedder=embed,
        )

        await cm._consolidate_session("s1", llm_extract, llm_decide)

    chroma_col.delete.assert_called_once_with(ids=["existing-id"])
    chroma_col.upsert.assert_not_called()


@pytest.mark.asyncio
async def test_trim_core_memory_preserves_goal_line():
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        redis = AsyncMock()
        # Goal line + many lines of content totalling >3000 tokens
        goal = "GOAL: do important work"
        filler = "\n".join([f"line {i}: " + "x" * 40 for i in range(400)])
        redis.get = AsyncMock(return_value=f"{goal}\n{filler}")
        redis.set = AsyncMock()

        cm = ContextManager(
            redis=redis, mongo_db=MagicMock(),
            chroma_cols={}, embedder=AsyncMock(),
        )

        await cm._trim_core_memory("s1")

    # Redis set was called with the trimmed value
    redis.set.assert_called_once()
    saved = redis.set.call_args[0][1]
    # Goal line is always first
    assert saved.startswith(goal)
    # Token count is under cap
    assert _mock_token_count(saved) <= 3000


@pytest.mark.asyncio
async def test_upsert_semantic_uses_sha1_id():
    import hashlib
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        chroma_col = AsyncMock()
        chroma_col.upsert = AsyncMock()
        embed = AsyncMock(return_value=[[0.1, 0.2, 0.3]])

        cm = ContextManager(
            redis=AsyncMock(), mongo_db=MagicMock(),
            chroma_cols={"semantic": chroma_col},
            embedder=embed,
        )

        await cm._upsert_semantic("sess-1", "user prefers tabs")

    expected_id = hashlib.sha1(b"sess-1:user prefers tabs").hexdigest()
    upsert_ids = chroma_col.upsert.call_args.kwargs["ids"]
    assert upsert_ids == [expected_id]
```

- [ ] **Step 2: Run tests to confirm FAIL**

```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest services/memory/tests/test_consolidation.py -v 2>&1 | tail -8
```

- [ ] **Step 3: Add consolidation methods to `context_manager.py`**

Add these imports at the top of `context_manager.py` (after existing imports):

```python
import hashlib
import time as _time
```

Add these methods to `ContextManager` (after `hybrid_retrieve`):

```python
async def consolidation_worker(
    self,
    llm_extract,
    llm_decide,
) -> None:
    """Background coroutine — reads from Redis Stream "consolidate", extracts
    semantic facts, reconciles them with existing archival memory, trims core.

    llm_extract: async (core_text: str) -> list[str]
    llm_decide:  async (candidate: str, similar: list[dict]) -> "ADD"|"UPDATE"|"DELETE"|"NOOP"

    Never called on the agent's hot path.
    """
    try:
        await self.redis.xgroup_create(
            "consolidate", "consolidation_workers", id="0", mkstream=True
        )
    except Exception:
        pass  # BUSYGROUP — group already exists

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
                    await self._consolidate_session(session_id, llm_extract, llm_decide)
                except Exception as exc:
                    pass  # log and continue — never crash the worker
                finally:
                    await self.redis.xack("consolidate", "consolidation_workers", entry_id)

async def _consolidate_session(
    self,
    session_id: str,
    llm_extract,
    llm_decide,
) -> None:
    """Extract facts from core memory, reconcile against semantic, trim cap."""
    core_text = await self.redis.get(f"core:{session_id}") or ""
    if not core_text.strip():
        return

    facts: list[str] = await llm_extract(core_text)

    for fact in facts:
        similar = await self.hybrid_retrieve(fact, collections=["semantic"], final_k=5)
        op: str = await llm_decide(fact, similar)

        if op in ("ADD", "UPDATE"):
            await self._upsert_semantic(session_id, fact)
        elif op == "DELETE":
            for s in similar:
                await self.chroma["semantic"].delete(ids=[s["id"]])
        # NOOP: do nothing

    await self._trim_core_memory(session_id)

async def _upsert_semantic(self, session_id: str, text: str) -> None:
    """Embed a fact and upsert into the semantic Chroma collection.

    Chroma ID is sha1(session_id:text) — idempotent across retries.
    """
    cid = hashlib.sha1(f"{session_id}:{text}".encode()).hexdigest()
    vec = (await self.embed([text]))[0]
    await self.chroma["semantic"].upsert(
        ids=[cid],
        embeddings=[vec],
        documents=[text],
        metadatas=[{
            "session_id":  session_id,
            "created_at":  _time.time(),
            "embed_model": "BAAI/bge-small-en-v1.5",
            "importance":  0.5,
            "source":      "consolidation",
        }],
    )

async def _trim_core_memory(self, session_id: str) -> None:
    """Evict oldest non-goal lines from core memory until under the 3,000-token cap.

    Line 0 is the pinned goal — it is NEVER evicted (Sisyphus Trap prevention).
    """
    CORE_CAP = 3_000
    key = f"core:{session_id}"
    text = await self.redis.get(key) or ""
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) <= 1:
        return
    pinned, evictable = lines[0], lines[1:]
    while token_count("\n".join([pinned] + evictable)) > CORE_CAP and evictable:
        evictable.pop(0)
    await self.redis.set(key, "\n".join([pinned] + evictable))
```

- [ ] **Step 4: Run consolidation tests — all 5 should pass**

```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest services/memory/tests/test_consolidation.py -v
```

- [ ] **Step 5: Run the complete memory test suite**

```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest services/memory/tests/ -v
```

Expected: 32 tests pass (15 original + 5 embedder + 3 reranker + 4 hybrid_retrieve + 5 consolidation).

---

## Self-Review

### Spec Coverage

| Spec requirement | Task |
|---|---|
| `embed()` — SentenceTransformer BAAI/bge-small-en-v1.5 on CUDA | Task 1 |
| `embed()` — Redis `embed_cache:{sha256}` with 3600s TTL | Task 1 |
| `embed()` — `asyncio.to_thread` for blocking encode | Task 1 |
| `embed()` — lazy singleton (`_MODEL = None` at import) | Task 1 |
| `rerank()` — FlagReranker BAAI/bge-reranker-v2-m3 fp16 | Task 2 |
| `rerank()` — lazy singleton, `asyncio.to_thread` | Task 2 |
| `hybrid_retrieve()` — dense Chroma query per collection | Task 3 |
| `hybrid_retrieve()` — BM25Okapi k1=1.5, b=0.75 on candidate set | Task 3 |
| `hybrid_retrieve()` — RRF fusion k=60 | Task 3 |
| `hybrid_retrieve()` — cross-encoder rerank via FlagReranker | Task 3 |
| `hybrid_retrieve()` — token budget packing (highest score first) | Task 3 |
| `build_context()` — RAG slot wired to `hybrid_retrieve()` | Task 4 |
| `consolidation_worker()` — `XREADGROUP` + `XACK` | Task 5 |
| `consolidation_worker()` — BUSYGROUP-safe `xgroup_create` | Task 5 |
| `_consolidate_session()` — `llm_extract` + `llm_decide` injection | Task 5 |
| ADD/UPDATE → `_upsert_semantic()` | Task 5 |
| DELETE → `chroma["semantic"].delete(ids=[...])` | Task 5 |
| NOOP → no-op | Task 5 |
| `_upsert_semantic()` — sha1 Chroma ID (idempotent) | Task 5 |
| `_trim_core_memory()` — line 0 pinned (Sisyphus Trap prevention) | Task 5 |
| `_trim_core_memory()` — 3,000 token cap | Task 5 |
| Worker errors logged but never crash the worker | Task 5 |

**Deferred (out of scope — requires orchestrator):**
- Real `llm_extract` / `llm_decide` callables (wired in orchestrator)
- Retrieval routing by memory type (SOTA item 2) — query classifier not yet built
- Importance-weighted decay / scheduled pruning (SOTA item 3)

### Placeholder Scan

None — all methods are complete implementations. The only stubs that remain are the injected LLM callables (`llm_extract`, `llm_decide`), which are intentionally injected dependencies, not missing code.

### Type Consistency

- `hybrid_retrieve()` returns `list[dict]` with keys `"id"`, `"text"`, `"score"` — `_consolidate_session()` reads `s["id"]` on DELETE. Match confirmed.
- `_upsert_semantic()` called with `(session_id: str, text: str)` — called as `_upsert_semantic(session_id, fact)` in `_consolidate_session`. Match confirmed.
- `consolidation_worker()` acks with `"consolidate"` stream + `"consolidation_workers"` group — `xgroup_create` uses same names. Match confirmed.
