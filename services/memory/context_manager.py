from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from services.memory.tokenizer import token_count

_logger = logging.getLogger(__name__)


@dataclass
class ContextBudget:
    max_tokens: int = 131072
    completion_reserve: int = 700
    agent_share: float = 0.08
    system_core_share: float = 0.22
    recent_turns_share: float = 0.30
    rag_share: float = 0.30
    summary_share: float = 0.10
    anchor_share: float = 0.03

    @property
    def effective_budget(self) -> int:
        return self.max_tokens - self.completion_reserve

    def slot(self, share: float) -> int:
        return int(self.effective_budget * share)


@dataclass
class AssembledContext:
    agent_instructions: str = ""
    system_prompt: str = ""
    core_memory: str = ""
    anchor_buffer: str = ""
    recent_turns: str = ""
    retrieved_context: str = ""
    summary_buffer: str = ""
    total_tokens: int = 0

    def as_prompt(self) -> str:
        """Assemble with highest-value content near head, recent turns near tail.

        Ordering: agent_instructions → system → core → anchor → RAG → summary → recent.
        agent_instructions (AGENT.md) is pinned at the very top and survives compaction.
        The anchor (founding facts) sits right after core memory so it stays visible
        even after many compact cycles dilute it out of the rolling summary.
        """
        return "\n\n".join(
            filter(
                None,
                [
                    self.agent_instructions,
                    self.system_prompt,
                    self.core_memory,
                    self.anchor_buffer,
                    self.retrieved_context,
                    self.summary_buffer,
                    self.recent_turns,
                ],
            )
        )


class ContextManager:
    def __init__(
        self,
        mongo_db,
        chroma_cols: dict,
        embedder,
        budget: ContextBudget | None = None,
        storage=None,
        local_store=None,
    ) -> None:
        self.db = mongo_db
        self.chroma = chroma_cols
        self.embed = embedder
        self.budget = budget or ContextBudget()
        self.storage = storage  # orchestrator StorageManager hook for importance boost
        self.local_store = local_store  # local SQLite store for chat-turn reads

    async def build_context(
        self,
        session_id: str,
        current_task: str,
        system_prompt: str,
        agent_instructions: str = "",
    ) -> AssembledContext:
        """Assemble the full context for one agent step, strictly within budget.

        agent_instructions (AGENT.md), system_prompt, and core memory are pinned
        and never trimmed. Only RAG, summary, and recent turns are budget-capped.
        """
        b = self.budget

        # 1. Pinned slots — agent_instructions + system_prompt + core never trimmed
        core = await self.local_store.session_kv_get("core", session_id) or ""
        pinned_tokens = (
            token_count(agent_instructions) + token_count(system_prompt) + token_count(core)
        )
        remaining = b.effective_budget - pinned_tokens

        # 2. RAG evidence — INERT since Piece 3 dropped Chroma (hybrid_retrieve
        #    returns []); the slot resolves to empty text. Kept as a no-op seam
        #    for a future local recall backend (SQLite-FTS).
        rag_budget = min(b.slot(b.rag_share), max(0, remaining))
        rag_chunks = await self.hybrid_retrieve(current_task, token_budget=rag_budget)
        await self._boost_retrieved(rag_chunks)
        rag_text = "\n\n".join(c["text"] for c in rag_chunks)
        remaining -= token_count(rag_text)

        # 3. Summary buffer
        summary_budget = min(b.slot(b.summary_share), max(0, remaining))
        summary = await self.local_store.session_kv_get("summary", session_id) or ""
        summary = self._trim_to_budget(summary, summary_budget)
        remaining -= token_count(summary)

        # 3b. Anchor buffer — surface founding facts only when they have drifted
        # out of the rolling summary, so the model keeps seeing them after many
        # compact cycles. Capped to a small dedicated slot.
        anchor_raw = await self.local_store.session_kv_get("anchor", session_id) or ""
        anchor_buffer = ""
        if self._anchor_diverges(anchor_raw, summary):
            anchor_budget = min(b.slot(b.anchor_share), max(0, remaining))
            anchor_body = self._trim_to_budget(anchor_raw, anchor_budget)
            if anchor_body:
                anchor_buffer = f"KEY FACTS (anchored, always relevant):\n{anchor_body}"
                remaining -= token_count(anchor_buffer)

        # 4. Recent turns (newest retained on trim)
        recent_budget = min(b.slot(b.recent_turns_share), max(0, remaining))
        recent = await self._recent_turns(session_id, recent_budget)

        ctx = AssembledContext(
            agent_instructions=agent_instructions,
            system_prompt=system_prompt,
            core_memory=core,
            anchor_buffer=anchor_buffer,
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
        lines = [line for line in text.splitlines() if line]
        while lines and token_count("\n".join(lines)) > budget:
            lines.pop(0)
        return "\n".join(lines)

    def _anchor_diverges(self, anchor: str, summary: str) -> bool:
        """True when the anchor carries facts not substantially present in summary.

        Cheap token-overlap heuristic (no LLM): if fewer than 60% of the anchor's
        distinct word tokens appear in the summary, the anchor has drifted out of
        the rolling summary and must be surfaced separately.
        """
        anchor = (anchor or "").strip()
        if not anchor:
            return False
        summary = (summary or "").strip()
        if not summary:
            return True
        anchor_words = {w for w in anchor.lower().split() if len(w) > 3}
        if not anchor_words:
            return False
        summary_words = {w for w in summary.lower().split() if len(w) > 3}
        overlap = len(anchor_words & summary_words) / len(anchor_words)
        return overlap < 0.60

    async def _recent_turns(self, session_id: str, budget: int) -> str:
        """Load recent turns from the local store (immutable store), filtered by watermark.

        Recent turns = turns with seq > watermark, where watermark is read from the
        local store's session_kv table. Watermark defaults to -1 (meaning all turns
        are recent on first access).
        """
        # Get watermark from the local store (defaults to -1)
        watermark_str = await self.local_store.session_kv_get("summarized_through", session_id)
        watermark = int(watermark_str) if watermark_str else -1

        # Read the NEWEST turns after the watermark from the local store —
        # it already applies seq>watermark, newest-cap, and ascending order.
        turns = await self.local_store.recent_turns(session_id, watermark=watermark, limit=50)

        # Format as "ROLE: text"
        lines = [f"{t['role'].upper()}: {t['text']}" for t in turns]
        return self._trim_to_budget("\n".join(lines), budget)

    async def hybrid_retrieve(
        self,
        query: str,
        collections: list[str] | None = None,
        top_k_first_stage: int = 50,
        final_k: int = 8,
        token_budget: int = 2_800,
    ) -> list[dict]:
        """RAG retrieval is inert (no Chroma-backed vector store) — always returns [].

        Kept as a no-op so build_context()'s call site remains unchanged (the
        RAG slot of the assembled prompt is simply empty). Continuity (recent
        turns, summary, anchor) is unaffected — see conversation_context/
        _recent_turns for the live continuity path.
        """
        return []

    async def _boost_retrieved(self, chunks: list[dict]) -> None:
        """No-op — importance-boost was semantic-memory/Chroma-only machinery.

        hybrid_retrieve always returns [] now, so this is never invoked with
        real chunks, but kept as a stable no-op call site.
        """
        return None

    # ── Compaction ────────────────────────────────────────────────────────────

    _MICRO_STRIP_THRESHOLD = 1500  # chars; strip old message content beyond this
    _TOOL_RESULT_THRESHOLD = 600  # chars; tool results stripped more aggressively
    _BLOCK_SIZE = 20  # turns per parallel summarization block
    _KEEP_RECENT = 15  # turns retained verbatim after full compact
    _KEEP_RECENT_TOOL_RESULTS = 10  # most-recent tool results never cleared (still referenced)
    _IDLE_COMPACT_SECONDS = 600  # idle threshold (s) before proactive background compact
    _LOW_FILL_RATIO = 0.50  # only background-compact when fill ratio exceeds this

    # Prompt used per block when summarizing in parallel
    _BLOCK_SUMMARY_PROMPT = (
        "Summarise the following conversation segment concisely.\n"
        "Preserve all decisions, key facts, file paths, error messages, and unresolved tasks.\n"
        "Discard greetings, repetitions, and small-talk.\n"
        "{anchor_section}"
        "SEGMENT:\n{history}\n\nSUMMARY:"
    )

    # Prompt used to merge parallel block summaries
    _MERGE_PROMPT = (
        "Merge the following segment summaries into one coherent summary.\n"
        "Resolve duplicates. Preserve key facts, decisions, file paths, and open tasks.\n"
        "Maintain chronological order.\n"
        "{anchor_section}"
        "SEGMENTS:\n{segments}\n\nMERGED SUMMARY:"
    )

    # Second LLM pass: extract durable insights for memory writing
    _REFLECTION_PROMPT = (
        "From this conversation summary, extract insights worth keeping in long-term memory.\n"
        "Return ONLY a JSON object with these keys (omit empty lists):\n"
        '  "decisions": [key decisions made],\n'
        '  "preferences": [user working-style or tool preferences revealed],\n'
        '  "findings": [key research findings or facts discovered],\n'
        '  "lessons": [things that failed and what was learned]\n'
        "Each item must be a single, self-contained sentence.\n\n"
        "SUMMARY:\n{summary}\n\nJSON:"
    )

    async def _parallel_summarize(
        self,
        turns: list[dict],
        anchor: str,
        llm_fn,
    ) -> str:
        """Summarize turns in parallel blocks, merge when >1 block.

        Parallel blocks eliminate the sequential stall on large histories.
        The anchor (key facts from the first compaction) is passed to every
        block and the merge so the summarizer cannot drift from established facts.
        """
        anchor_section = f"FIXED CONTEXT (always preserve):\n{anchor}\n\n" if anchor else ""

        # Split turns into fixed-size blocks
        blocks = [turns[i : i + self._BLOCK_SIZE] for i in range(0, len(turns), self._BLOCK_SIZE)]

        async def _summarize_block(block: list[dict]) -> str:
            history = "\n".join(f"{t['role'].upper()}: {t['content']}" for t in block)
            prompt = self._BLOCK_SUMMARY_PROMPT.format(
                anchor_section=anchor_section,
                history=history,
            )
            return await llm_fn(prompt)

        summaries = await asyncio.gather(*[_summarize_block(b) for b in blocks])

        if len(summaries) == 1:
            return summaries[0]

        # Merge block summaries into one coherent summary
        segments = "\n\n---\n\n".join(f"Segment {i + 1}:\n{s}" for i, s in enumerate(summaries))
        return await llm_fn(
            self._MERGE_PROMPT.format(
                anchor_section=anchor_section,
                segments=segments,
            )
        )

    async def _extract_reflections(self, summary: str, llm_fn) -> list[str]:
        """Second LLM pass: extract durable insights for memory writing.

        Returns a flat list of insight strings (decisions, preferences,
        findings, lessons). Never raises — reflections are best-effort.
        """
        import json as _json

        try:
            raw = await llm_fn(self._REFLECTION_PROMPT.format(summary=summary[:8_000]))
            start, end = raw.find("{"), raw.rfind("}") + 1
            if start < 0 or end <= start:
                return []
            data = _json.loads(raw[start:end])
            insights: list[str] = []
            for key in ("decisions", "preferences", "findings", "lessons"):
                insights.extend(data.get(key, []))
            return [s for s in insights if isinstance(s, str) and s.strip()]
        except Exception:
            return []

    async def full_compact(self, session_id: str, llm_fn) -> dict:
        """Summarise old turns with anchoring and parallel blocks → advance watermark (non-destructive).

        Improvements over naive summarization:
          1. Watermark-based non-destructive — advances a local-store watermark instead
             of deleting
          2. Parallel blocks — concurrent summarization removes blocking stall
          3. Anchoring — first summary stored as anchor; passed to all subsequent
             compactions so the model cannot drift from early established facts
          4. Reflection extraction — second LLM pass extracts durable insights
             returned to the caller for writing to episodic/semantic memory

        llm_fn: async (prompt: str) -> str
        Returns {summary_tokens, pruned_messages, reflections: list[str]}.
        """
        # Step 1: read all turns from the local store (immutable source)
        all_turns = await self.local_store.all_turns(session_id)
        if len(all_turns) <= self._KEEP_RECENT:
            return {"summary_tokens": 0, "pruned_messages": 0, "reflections": []}

        # Step 2: determine watermark and which turns are eligible for compaction
        watermark_str = await self.local_store.session_kv_get("summarized_through", session_id)
        watermark = int(watermark_str) if watermark_str else -1
        max_seq = all_turns[-1].get("seq", -1) if all_turns else -1

        # to_compact = turns with seq > watermark AND seq <= (max_seq - _KEEP_RECENT)
        # i.e., older than the recent tail that isn't already summarized
        threshold_seq = max_seq - self._KEEP_RECENT
        to_compact = [
            t
            for t in all_turns
            if t.get("seq", -1) > watermark and t.get("seq", -1) <= threshold_seq
        ]

        if not to_compact:
            return {"summary_tokens": 0, "pruned_messages": 0, "reflections": []}

        # Step 3: pre-compaction token total (denominator for compression ratio)
        pre_tokens = token_count(
            "\n".join(f"{t.get('role', '').upper()}: {t.get('text', '')}" for t in to_compact)
        )

        # Step 4: load anchor (stable early-session facts; empty on first compact)
        anchor = await self.local_store.session_kv_get("anchor", session_id) or ""

        # Step 5: parallel anchored summarization (map text→content for _parallel_summarize)
        to_compact_mapped = [
            {"role": t.get("role", ""), "content": t.get("text", "")} for t in to_compact
        ]
        new_summary = await self._parallel_summarize(to_compact_mapped, anchor, llm_fn)

        # Step 6: first compact → save result as the session anchor
        if not anchor:
            await self.local_store.session_kv_set("anchor", session_id, new_summary)

        # Step 7: merge new summary into existing summary (or replace if none exists)
        old_summary = await self.local_store.session_kv_get("summary", session_id) or ""

        if old_summary:
            # Merge: send both summaries to the model
            segments = f"Segment 1:\n{old_summary}\n\n---\n\nSegment 2:\n{new_summary}"
            anchor_section = f"FIXED CONTEXT (always preserve):\n{anchor}\n\n" if anchor else ""
            merged_summary = await llm_fn(
                self._MERGE_PROMPT.format(
                    anchor_section=anchor_section,
                    segments=segments,
                )
            )
            final_summary = merged_summary
        else:
            # First-ever summary
            final_summary = new_summary

        # Step 8: persist the merged summary to the local store (rollback on failure)
        try:
            await self.local_store.session_kv_set("summary", session_id, final_summary)
        except Exception:
            if old_summary:
                await self.local_store.session_kv_set("summary", session_id, old_summary)
            else:
                await self.local_store.session_kv_delete("summary", session_id)
            raise

        # Step 9: advance watermark to max_seq in to_compact (rollback on failure)
        max_compacted_seq = max([t.get("seq", -1) for t in to_compact])
        try:
            await self.local_store.session_kv_set(
                "summarized_through", session_id, str(max_compacted_seq)
            )
        except Exception:
            # Rollback summary
            if old_summary:
                await self.local_store.session_kv_set("summary", session_id, old_summary)
            else:
                await self.local_store.session_kv_delete("summary", session_id)
            raise

        # Step 10: extract reflections for the caller to write to memory
        reflections = await self._extract_reflections(final_summary, llm_fn)

        summary_tokens = token_count(final_summary)
        tokens_saved = max(0, pre_tokens - summary_tokens)
        compression_ratio = round(min(1.0, summary_tokens / pre_tokens), 4) if pre_tokens else 0.0

        # Step 11: emit compaction quality for frontend instrumentation (best-effort).
        try:
            from services.orchestrator import events as _events

            await _events.emit(
                "compact.quality",
                session_id=session_id,
                compression_ratio=compression_ratio,
                turns_compacted=len(to_compact),
                tokens_saved=tokens_saved,
                reflections_count=len(reflections),
            )
        except Exception:
            _logger.debug("compact.quality emit skipped", exc_info=True)

        return {
            "summary_tokens": summary_tokens,
            "pruned_messages": 0,  # no deletion; watermark-only
            "reflections": reflections,
        }

    async def conversation_context(self, session_id: str, budget: int | None = None) -> str:
        """Assemble conversation continuity block for multi-turn context.

        Returns summary + anchor (if diverged) + recent turns, formatted as a string.
        NO RAG/hybrid_retrieve — this is the hot path, keep it cheap.
        Best-effort: returns "" on any failure (never breaks the loop).

        budget: token limit for the assembled block. Defaults to a sane
                percentage of the total context budget if not provided.
        """
        try:
            if not session_id:
                return ""

            # Use a reasonable default budget if not provided
            if budget is None:
                budget = self.budget.slot(self.budget.recent_turns_share)

            # Read summary from the local store
            summary = await self.local_store.session_kv_get("summary", session_id) or ""

            # Read anchor, include only if it diverges from summary
            anchor_raw = await self.local_store.session_kv_get("anchor", session_id) or ""
            anchor_block = ""
            if anchor_raw and self._anchor_diverges(anchor_raw, summary):
                anchor_block = f"KEY FACTS (anchored, always relevant):\n{anchor_raw}"

            # Read recent turns (non-destructive, watermark-based)
            recent = await self._recent_turns(session_id, budget)

            # Assemble: summary + anchor + recent
            parts = [p for p in [summary, anchor_block, recent] if p]
            return "\n\n".join(parts) if parts else ""
        except Exception:
            # Best-effort: never break the loop on memory failure
            _logger.debug("conversation_context failed (returning empty string)", exc_info=True)
            return ""

    async def last_activity_seconds(self, session_id: str) -> float:
        """Seconds since the newest turn in this session was written.

        Reads the newest turn's createdAt from the local store (ISO string format).
        Returns 0.0 when the session has no turns or the field is absent/unparseable — i.e.
        "not idle", so a missing timestamp never triggers a surprise compaction.
        """
        newest_iso = await self.local_store.last_activity_iso(session_id)
        if not newest_iso:
            return 0.0

        # Parse ISO 8601 string (format: "2026-01-15T10:30:45Z")
        try:
            newest = datetime.strptime(newest_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            return max(0.0, (datetime.now(UTC) - newest).total_seconds())
        except (ValueError, TypeError):
            return 0.0

    async def maybe_background_compact(
        self,
        session_id: str,
        llm_fn,
        system_prompt: str = "",
        agent_instructions: str = "",
    ) -> dict | None:
        """Compact proactively iff the session is idle AND context fill is high.

        Returns the full_compact result dict when a compaction ran, else None.
        Idle gate prevents racing an in-flight task; the LOW fill gate prevents
        wasting an LLM call on a near-empty session. Never raises — background
        callers treat None as "nothing to do".
        """
        try:
            idle = await self.last_activity_seconds(session_id)
            if idle < self._IDLE_COMPACT_SECONDS:
                return None

            ctx = await self.build_context(
                session_id=session_id,
                current_task="",
                system_prompt=system_prompt,
                agent_instructions=agent_instructions,
            )
            low_thresh = int(self.budget.effective_budget * self._LOW_FILL_RATIO)
            if ctx.total_tokens < low_thresh:
                return None

            _logger.info(
                "background compact: session %s idle %.0fs, fill %d/%d",
                session_id,
                idle,
                ctx.total_tokens,
                self.budget.effective_budget,
            )
            return await self.full_compact(session_id, llm_fn)
        except Exception:
            _logger.warning("background compact failed (non-fatal)", exc_info=True)
            return None
