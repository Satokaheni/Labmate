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

        # 2. RAG evidence (stub — hybrid_retrieve is Plan B)
        rag_text  = ""  # Plan B: await self.hybrid_retrieve(current_task, rag_budget)
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
