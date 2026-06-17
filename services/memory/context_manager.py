from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field

from services.memory.tokenizer import token_count
from services.memory.reranker import rerank
from rank_bm25 import BM25Okapi

_logger = logging.getLogger(__name__)


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
    system_prompt:     str
    core_memory:       str
    recent_turns:      str
    retrieved_context: str
    summary_buffer:    str
    total_tokens:      int = 0

    def as_prompt(self) -> str:
        """Assemble with highest-value content near head, recent turns near tail.

        Ordering: system → core → RAG evidence → summary → recent turns.
        Positions RAG context near head and recency near completion point,
        countering lost-in-the-middle degradation.
        """
        return "\n\n".join(filter(None, [
            self.system_prompt,
            self.core_memory,
            self.retrieved_context,
            self.summary_buffer,
            self.recent_turns,
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
        """Assemble the full context for one agent step, strictly within budget.

        Core memory is pinned and never trimmed. Only summary and recent turns
        are subject to trimming when the budget is tight.
        """
        b = self.budget

        # 1. Pinned slots — system prompt + core memory are never trimmed
        core = await self.redis.get(f"core:{session_id}") or ""

        sys_core_tokens = token_count(system_prompt) + token_count(core)
        remaining = b.effective_budget - sys_core_tokens

        # 2. RAG evidence — hybrid BM25 + dense → RRF → rerank
        rag_budget = min(int(b.effective_budget * b.rag_share), max(0, remaining))
        rag_chunks = await self.hybrid_retrieve(current_task, token_budget=rag_budget)
        rag_text   = "\n\n".join(c["text"] for c in rag_chunks)
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
        if not text or token_count(text) <= budget:
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
                        _logger.exception("consolidation error for session %s: %s", session_id, exc)
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
                "created_at":  time.time(),
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
