# Smarter Compaction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend Labmate's compaction system with anchor-integrated context building, age-aware tool-result clearing, background proactive compaction, and compaction quality metrics.

**Architecture:** The existing full_compact pipeline (clear_tool_results → parallel blocks → anchor → reflection extraction) is complete. This plan adds: (1) anchor surfacing in build_context so anchored facts reach the model after many compact cycles; (2) age-aware tool result clearing to preserve recent tool outputs; (3) a background asyncio task that compacts idle sessions proactively; (4) a compact.quality event for frontend instrumentation.

**Tech Stack:** Python, asyncio, Motor (async MongoDB), redis.asyncio, litellm (Gemma 4 31B), pytest + pytest-asyncio

---

## Baseline (already implemented — DO NOT redesign)

These exist and are covered by tests. Treat them as fixed contracts the new work builds on.

- [x] `services/memory/context_manager.py` — `ContextManager` with `build_context`, `microcompact`, `clear_tool_results`, `_parallel_summarize`, `_extract_reflections`, `full_compact`, `consolidation_worker`.
- [x] Compaction constants: `_MICRO_STRIP_THRESHOLD = 1500`, `_TOOL_RESULT_THRESHOLD = 600`, `_BLOCK_SIZE = 20`, `_KEEP_RECENT = 15`.
- [x] Redis keys: `summary:{session_id}`, `anchor:{session_id}`, `core:{session_id}`. Anchor saved only on first compact; summary REPLACED each compact.
- [x] `services/memory/tests/test_context_manager.py` — `test_clear_tool_results_strips_large_tool_messages`, `test_full_compact_returns_reflections`, `test_full_compact_saves_anchor_only_on_first_compact`, `test_parallel_summarize_calls_llm_per_block`, plus the `_AsyncIter` / `_make_cm` / `_mock_token_count` test helpers.
- [x] `services/orchestrator/main.py` — manual compact (`kind == "compact"`) and auto-compact (when `ctx_check.total_tokens >= FULL_THRESH`) both call `storage.context_manager.full_compact(...)` and fire `storage.consolidator.write_reflections(...)` in `asyncio.create_task`. Thresholds: `MICRO_THRESH = CTX_TOKENS * 0.70`, `FULL_THRESH = CTX_TOKENS * 0.85`, `CTX_TOKENS = 131072`.
- [x] `services/orchestrator/events.py` — module-level `await events.emit(type, **fields)` routes to the task-scoped `current_emitter` ContextVar; no-op when unset; best-effort (never raises).

---

## Task 1 — Add `LOW_THRESH` and age-aware `clear_tool_results`

**Why:** `clear_tool_results` currently clears EVERY `role='tool'` message over 600 chars regardless of age. Recent tool outputs (last 10) are often still referenced by the next turn. Skip the 10 most-recent tool results, clear only older ones. Also introduce a `LOW_THRESH` constant on `ContextManager` reused by background compaction (Task 3) so all thresholds live in one place.

**Files:**
- Modify: `/Users/zachstallbohm/Work/Labmate/services/memory/context_manager.py`
- Test: `/Users/zachstallbohm/Work/Labmate/services/memory/tests/test_context_manager.py`

### Steps

- [ ] Add a `_KEEP_RECENT_TOOL_RESULTS` constant next to the other compaction constants in `context_manager.py`. Find this block:

```python
    _MICRO_STRIP_THRESHOLD = 1500   # chars; strip old message content beyond this
    _TOOL_RESULT_THRESHOLD = 600    # chars; tool results stripped more aggressively
    _BLOCK_SIZE            = 20     # turns per parallel summarization block
    _KEEP_RECENT           = 15     # turns retained verbatim after full compact
```

Replace it with:

```python
    _MICRO_STRIP_THRESHOLD = 1500   # chars; strip old message content beyond this
    _TOOL_RESULT_THRESHOLD = 600    # chars; tool results stripped more aggressively
    _BLOCK_SIZE            = 20     # turns per parallel summarization block
    _KEEP_RECENT           = 15     # turns retained verbatim after full compact
    _KEEP_RECENT_TOOL_RESULTS = 10  # most-recent tool results never cleared (still referenced)
```

- [ ] Rewrite `clear_tool_results` to skip the most-recent N tool results. Find the current method body (the cursor + `async for` loop) and replace the whole method with:

```python
    async def clear_tool_results(self, session_id: str) -> int:
        """Replace large tool-result bodies with stubs before LLM summarization.

        Targets role='tool' messages specifically, leaving conversation intact.
        In long research sessions tool outputs (file reads, web search dumps)
        dominate token usage; clearing them before summarization dramatically
        reduces the LLM input without losing conversational context.

        Age-aware: the _KEEP_RECENT_TOOL_RESULTS most-recent tool results are
        left intact (the user / next turn may still refer to them); only older
        tool results over the threshold are cleared.
        Returns chars freed.
        """
        cursor = (
            self.db["messages"]
            .find(
                {
                    "session_id": session_id,
                    "role": "tool",
                    "stripped": {"$ne": True},
                },
                {"_id": 1, "content": 1},
            )
            .sort("seq", -1)
            .skip(self._KEEP_RECENT_TOOL_RESULTS)
        )
        freed = 0
        async for doc in cursor:
            content = doc.get("content", "")
            if len(content) > self._TOOL_RESULT_THRESHOLD:
                stub = f"[tool result: {len(content)} chars cleared]"
                await self.db["messages"].update_one(
                    {"_id": doc["_id"]},
                    {"$set": {"content": stub, "stripped": True}},
                )
                freed += len(content) - len(stub)
        return freed
```

- [ ] The existing test `test_clear_tool_results_strips_large_tool_messages` uses `_AsyncIter`, which already supports `.sort()` and `.skip()` (both return `self`). Confirm by reading the `_AsyncIter` helper — its `sort` and `skip` methods both `return self`. No change needed to that test; it still passes because `.skip(10)` over a 2-element iterator is a no-op in the mock (the mock iterator does not actually skip), so update_one is still called once for the large doc.

- [ ] Add a new test that proves recent tool results are preserved. Append to `/Users/zachstallbohm/Work/Labmate/services/memory/tests/test_context_manager.py`:

```python
@pytest.mark.asyncio
async def test_clear_tool_results_skips_recent_tool_results():
    """The _KEEP_RECENT_TOOL_RESULTS most-recent tool results are not cleared.

    The mock cursor records the skip() argument so we assert age-awareness is
    wired through to the DB query (Motor applies .skip server-side).
    """
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)

        recorded = {"skip": None, "sort": None}

        class RecordingCursor(_AsyncIter):
            def sort(self, *args, **kwargs):
                recorded["sort"] = args
                return self
            def skip(self, n, *args, **kwargs):
                recorded["skip"] = n
                return self

        db = MagicMock()
        # Only "old" docs reach the iterator (the mock simulates skip server-side
        # by simply not including the recent ones).
        old_docs = [{"_id": "old1", "content": "x" * 1000}]
        messages_col = MagicMock()
        messages_col.find = MagicMock(return_value=RecordingCursor(old_docs))
        messages_col.update_one = AsyncMock()
        db.__getitem__ = MagicMock(return_value=messages_col)

        cm = ContextManager(redis=redis, mongo_db=db, chroma_cols={}, embedder=AsyncMock())
        freed = await cm.clear_tool_results("s1")

        assert recorded["skip"] == cm._KEEP_RECENT_TOOL_RESULTS
        assert recorded["skip"] == 10
        assert freed > 0
        assert messages_col.update_one.call_count == 1
```

- [ ] Run the tests:

```bash
cd /Users/zachstallbohm/Work/Labmate && python -m pytest services/memory/tests/test_context_manager.py -x -q
```

- [ ] Commit:

```bash
cd /Users/zachstallbohm/Work/Labmate && git add services/memory/context_manager.py services/memory/tests/test_context_manager.py && git commit -m "feat(memory): age-aware clear_tool_results keeps 10 most-recent tool outputs

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2 — Surface the anchor in `build_context`

**Why:** `build_context` reads `summary:{session_id}` but never `anchor:{session_id}`. After many compact cycles the early established facts (the anchor) can be diluted out of the rolling summary. When the anchor diverges from the current summary, prepend a short, clearly-labelled anchor section so the model keeps seeing the founding facts. The anchor must be cheap: cap it to a small token budget and only include it when it is NOT already substantially contained in the summary.

**Files:**
- Modify: `/Users/zachstallbohm/Work/Labmate/services/memory/context_manager.py`
- Test: `/Users/zachstallbohm/Work/Labmate/services/memory/tests/test_context_manager.py`

### Steps

- [ ] Add an `anchor_share` field to `ContextBudget`. Find the dataclass:

```python
@dataclass
class ContextBudget:
    max_tokens:          int   = 131072
    completion_reserve:  int   = 700
    agent_share:         float = 0.08
    system_core_share:   float = 0.22
    recent_turns_share:  float = 0.30
    rag_share:           float = 0.30
    summary_share:       float = 0.10
```

Replace with:

```python
@dataclass
class ContextBudget:
    max_tokens:          int   = 131072
    completion_reserve:  int   = 700
    agent_share:         float = 0.08
    system_core_share:   float = 0.22
    recent_turns_share:  float = 0.30
    rag_share:           float = 0.30
    summary_share:       float = 0.10
    anchor_share:        float = 0.03
```

- [ ] Add an `anchor_buffer` field to `AssembledContext` and surface it in `as_prompt()`. Find:

```python
@dataclass
class AssembledContext:
    agent_instructions: str = ""
    system_prompt:      str = ""
    core_memory:        str = ""
    recent_turns:       str = ""
    retrieved_context:  str = ""
    summary_buffer:     str = ""
    total_tokens:       int = 0

    def as_prompt(self) -> str:
        """Assemble with highest-value content near head, recent turns near tail.

        Ordering: agent_instructions → system → core → RAG → summary → recent turns.
        agent_instructions (AGENT.md) is pinned at the very top and survives compaction.
        """
        return "\n\n".join(filter(None, [
            self.agent_instructions,
            self.system_prompt,
            self.core_memory,
            self.retrieved_context,
            self.summary_buffer,
            self.recent_turns,
        ]))
```

Replace with:

```python
@dataclass
class AssembledContext:
    agent_instructions: str = ""
    system_prompt:      str = ""
    core_memory:        str = ""
    anchor_buffer:      str = ""
    recent_turns:       str = ""
    retrieved_context:  str = ""
    summary_buffer:     str = ""
    total_tokens:       int = 0

    def as_prompt(self) -> str:
        """Assemble with highest-value content near head, recent turns near tail.

        Ordering: agent_instructions → system → core → anchor → RAG → summary → recent.
        agent_instructions (AGENT.md) is pinned at the very top and survives compaction.
        The anchor (founding facts) sits right after core memory so it stays visible
        even after many compact cycles dilute it out of the rolling summary.
        """
        return "\n\n".join(filter(None, [
            self.agent_instructions,
            self.system_prompt,
            self.core_memory,
            self.anchor_buffer,
            self.retrieved_context,
            self.summary_buffer,
            self.recent_turns,
        ]))
```

- [ ] Add an anchor-divergence helper method to `ContextManager`. Insert it immediately after `_trim_to_budget` (before `_recent_turns`):

```python
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
```

- [ ] Wire the anchor into `build_context`. Find the summary-buffer section and the `AssembledContext(...)` construction:

```python
        # 3. Summary buffer
        summary_budget = min(b.slot(b.summary_share), max(0, remaining))
        summary = await self.redis.get(f"summary:{session_id}") or ""
        summary = self._trim_to_budget(summary, summary_budget)
        remaining -= token_count(summary)

        # 4. Recent turns (newest retained on trim)
        recent_budget = min(b.slot(b.recent_turns_share), max(0, remaining))
        recent = await self._recent_turns(session_id, recent_budget)

        ctx = AssembledContext(
            agent_instructions=agent_instructions,
            system_prompt=system_prompt,
            core_memory=core,
            recent_turns=recent,
            retrieved_context=rag_text,
            summary_buffer=summary,
        )
```

Replace with:

```python
        # 3. Summary buffer
        summary_budget = min(b.slot(b.summary_share), max(0, remaining))
        summary = await self.redis.get(f"summary:{session_id}") or ""
        summary = self._trim_to_budget(summary, summary_budget)
        remaining -= token_count(summary)

        # 3b. Anchor buffer — surface founding facts only when they have drifted
        # out of the rolling summary, so the model keeps seeing them after many
        # compact cycles. Capped to a small dedicated slot.
        anchor_raw = await self.redis.get(f"anchor:{session_id}") or ""
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
```

- [ ] Add tests. Append to `/Users/zachstallbohm/Work/Labmate/services/memory/tests/test_context_manager.py`:

```python
@pytest.mark.asyncio
async def test_build_context_surfaces_anchor_when_diverged():
    """When the anchor's facts are absent from the summary, anchor_buffer is populated."""
    with (
        patch("services.memory.context_manager.token_count", side_effect=_mock_token_count),
        patch("services.memory.context_manager.rerank", new_callable=AsyncMock, return_value=[]),
    ):
        from services.memory.context_manager import ContextManager, ContextBudget

        store = {
            "core:s1": "GOAL: build MCP bridge",
            "summary:s1": "discussed unrelated weather topics today",
            "anchor:s1": "project uses Gemma model with Redis streams for goals",
        }
        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=lambda k: store.get(k))

        db = MagicMock()

        class EmptyCursor:
            def __aiter__(self): return self
            async def __anext__(self): raise StopAsyncIteration

        mock_sort = MagicMock()
        mock_sort.limit = MagicMock(return_value=EmptyCursor())
        mock_find = MagicMock()
        mock_find.sort = MagicMock(return_value=mock_sort)
        db.messages.find = MagicMock(return_value=mock_find)

        embed = AsyncMock(return_value=[[0.1]])
        budget = ContextBudget(max_tokens=4000, completion_reserve=100)
        cm = ContextManager(redis=redis, mongo_db=db, chroma_cols={}, embedder=embed, budget=budget)

        ctx = await cm.build_context("s1", "task", "system")
        assert "KEY FACTS" in ctx.anchor_buffer
        assert "Gemma" in ctx.anchor_buffer
        assert "KEY FACTS" in ctx.as_prompt()


@pytest.mark.asyncio
async def test_build_context_omits_anchor_when_contained_in_summary():
    """When the summary already contains the anchor's facts, anchor_buffer is empty."""
    with (
        patch("services.memory.context_manager.token_count", side_effect=_mock_token_count),
        patch("services.memory.context_manager.rerank", new_callable=AsyncMock, return_value=[]),
    ):
        from services.memory.context_manager import ContextManager, ContextBudget

        store = {
            "core:s1": "GOAL: build MCP bridge",
            "summary:s1": "project uses Gemma model with Redis streams for goals and more detail",
            "anchor:s1": "project uses Gemma model with Redis streams for goals",
        }
        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=lambda k: store.get(k))

        db = MagicMock()

        class EmptyCursor:
            def __aiter__(self): return self
            async def __anext__(self): raise StopAsyncIteration

        mock_sort = MagicMock()
        mock_sort.limit = MagicMock(return_value=EmptyCursor())
        mock_find = MagicMock()
        mock_find.sort = MagicMock(return_value=mock_sort)
        db.messages.find = MagicMock(return_value=mock_find)

        embed = AsyncMock(return_value=[[0.1]])
        budget = ContextBudget(max_tokens=4000, completion_reserve=100)
        cm = ContextManager(redis=redis, mongo_db=db, chroma_cols={}, embedder=embed, budget=budget)

        ctx = await cm.build_context("s1", "task", "system")
        assert ctx.anchor_buffer == ""
```

- [ ] Update `test_assembled_context_as_prompt_ordering` so it still asserts correct ordering with the new anchor slot. Find:

```python
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
        rag_pos     = prompt.index("rag")
        summary_pos = prompt.index("summary")
        recent_pos  = prompt.index("recent")
        assert rag_pos < summary_pos < recent_pos
```

Replace with:

```python
def test_assembled_context_as_prompt_ordering():
    """core → anchor → RAG → summary → recent turns ordering is preserved."""
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import AssembledContext
        ctx = AssembledContext(
            system_prompt="sys",
            core_memory="goalcore",
            anchor_buffer="anchorfacts",
            recent_turns="recenttext",
            retrieved_context="ragtext",
            summary_buffer="summarytext",
        )
        prompt = ctx.as_prompt()
        core_pos    = prompt.index("goalcore")
        anchor_pos  = prompt.index("anchorfacts")
        rag_pos     = prompt.index("ragtext")
        summary_pos = prompt.index("summarytext")
        recent_pos  = prompt.index("recenttext")
        assert core_pos < anchor_pos < rag_pos < summary_pos < recent_pos
```

- [ ] Run the tests:

```bash
cd /Users/zachstallbohm/Work/Labmate && python -m pytest services/memory/tests/test_context_manager.py -x -q
```

- [ ] Commit:

```bash
cd /Users/zachstallbohm/Work/Labmate && git add services/memory/context_manager.py services/memory/tests/test_context_manager.py && git commit -m "feat(memory): surface diverged anchor facts in build_context

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3 — Emit `compact.quality` event from `full_compact`

**Why:** The frontend context bar wants compaction quality over time. `full_compact` already knows `summary_tokens` and `pruned_messages` and computes reflections. Add a `compression_ratio` and emit a `compact.quality` event so the bar can chart it. Emission must be best-effort and must NOT change the return contract (`{summary_tokens, pruned_messages, reflections}`) that the orchestrator relies on.

The event uses the module-level `events.emit` from `services/orchestrator/events.py`, which is a no-op when no task emitter is set (so it is safe to call from background contexts and from tests). To avoid a hard import-time dependency of the memory service on the orchestrator, import `events` lazily inside `full_compact`.

**Files:**
- Modify: `/Users/zachstallbohm/Work/Labmate/services/memory/context_manager.py`
- Test: `/Users/zachstallbohm/Work/Labmate/services/memory/tests/test_context_manager.py`

### Steps

- [ ] In `full_compact`, capture the pre-compaction token total so a ratio can be computed. Find the start of the method body after tool clearing:

```python
        # Step 1: clear tool results first (zero-LLM, maximum token recovery)
        await self.clear_tool_results(session_id)

        cursor = (
            self.db["messages"]
            .find(
                {"session_id": session_id},
                {"_id": 1, "seq": 1, "role": 1, "content": 1},
            )
            .sort("seq", 1)
        )
        all_turns = [doc async for doc in cursor]
        if len(all_turns) <= self._KEEP_RECENT:
            return {"summary_tokens": 0, "pruned_messages": 0, "reflections": []}

        to_compact = all_turns[: -self._KEEP_RECENT]
```

Replace with:

```python
        # Step 1: clear tool results first (zero-LLM, maximum token recovery)
        await self.clear_tool_results(session_id)

        cursor = (
            self.db["messages"]
            .find(
                {"session_id": session_id},
                {"_id": 1, "seq": 1, "role": 1, "content": 1},
            )
            .sort("seq", 1)
        )
        all_turns = [doc async for doc in cursor]
        if len(all_turns) <= self._KEEP_RECENT:
            return {"summary_tokens": 0, "pruned_messages": 0, "reflections": []}

        to_compact = all_turns[: -self._KEEP_RECENT]

        # Pre-compaction token total of the turns we are about to replace — the
        # denominator for the compression ratio reported in compact.quality.
        pre_tokens = token_count(
            "\n".join(
                f"{t.get('role', '').upper()}: {t.get('content', '')}"
                for t in to_compact
            )
        )
```

- [ ] At the end of `full_compact`, compute the ratio, emit the event, and keep the return value unchanged. Find:

```python
        # Step 7: extract reflections for the caller to write to memory
        reflections = await self._extract_reflections(summary, llm_fn)

        return {
            "summary_tokens": token_count(summary),
            "pruned_messages": len(ids),
            "reflections": reflections,
        }
```

Replace with:

```python
        # Step 7: extract reflections for the caller to write to memory
        reflections = await self._extract_reflections(summary, llm_fn)

        summary_tokens = token_count(summary)
        tokens_saved = max(0, pre_tokens - summary_tokens)
        compression_ratio = round(summary_tokens / pre_tokens, 4) if pre_tokens else 0.0

        # Step 8: emit compaction quality for frontend instrumentation (best-effort).
        # Lazy import keeps the memory service free of a hard orchestrator dependency;
        # events.emit is a no-op when no task emitter is set (background/tests).
        try:
            from services.orchestrator import events as _events
            await _events.emit(
                "compact.quality",
                session_id=session_id,
                compression_ratio=compression_ratio,
                turns_compacted=len(ids),
                tokens_saved=tokens_saved,
                reflections_count=len(reflections),
            )
        except Exception:
            _logger.debug("compact.quality emit skipped", exc_info=True)

        return {
            "summary_tokens": summary_tokens,
            "pruned_messages": len(ids),
            "reflections": reflections,
        }
```

- [ ] Add a test asserting the event payload. Append to `/Users/zachstallbohm/Work/Labmate/services/memory/tests/test_context_manager.py`:

```python
@pytest.mark.asyncio
async def test_full_compact_emits_compact_quality_event():
    """full_compact emits compact.quality with ratio, counts, and tokens saved."""
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        redis_store: dict = {}
        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=lambda k: redis_store.get(k))
        redis.set = AsyncMock(side_effect=lambda k, v: redis_store.update({k: v}))
        redis.delete = AsyncMock(side_effect=lambda k: redis_store.pop(k, None))

        db = MagicMock()
        turns = [
            {"_id": str(i), "seq": i, "role": "user" if i % 2 == 0 else "assistant",
             "content": f"message number {i} with some content to make tokens"}
            for i in range(20)
        ]
        messages_col = MagicMock()
        call_count = [0]
        def _find(*args, **kwargs):
            call_count[0] += 1
            return _AsyncIter([] if call_count[0] == 1 else turns)
        messages_col.find = MagicMock(side_effect=_find)
        messages_col.update_one = AsyncMock()
        messages_col.delete_many = AsyncMock()
        db.__getitem__ = MagicMock(return_value=messages_col)

        async def _llm(prompt: str) -> str:
            return '{"decisions": ["use Python"]}' if "JSON" in prompt else "short summary"

        captured = {}
        async def _fake_emit(type, **fields):
            captured[type] = fields

        cm = ContextManager(redis=redis, mongo_db=db, chroma_cols={}, embedder=AsyncMock())

        with patch("services.orchestrator.events.emit", side_effect=_fake_emit):
            result = await cm.full_compact("s1", _llm)

        assert "compact.quality" in captured
        evt = captured["compact.quality"]
        assert evt["session_id"] == "s1"
        assert evt["turns_compacted"] == 5
        assert evt["reflections_count"] == 1
        assert evt["tokens_saved"] >= 0
        assert 0.0 <= evt["compression_ratio"]
        # Return contract is unchanged
        assert result["pruned_messages"] == 5
        assert result["reflections"] == ["use Python"]
```

- [ ] Run the tests:

```bash
cd /Users/zachstallbohm/Work/Labmate && python -m pytest services/memory/tests/test_context_manager.py -x -q
```

- [ ] Commit:

```bash
cd /Users/zachstallbohm/Work/Labmate && git add services/memory/context_manager.py services/memory/tests/test_context_manager.py && git commit -m "feat(memory): emit compact.quality event from full_compact

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 4 — Idle detection + proactive compaction on `ContextManager`

**Why:** Compaction only fires when a message arrives and context is already full. Add the building blocks for proactive compaction directly on `ContextManager` so they are unit-testable without the orchestrator: (a) a `last_activity_seconds` method that reports how long a session has been idle (from the newest message's timestamp), and (b) a `maybe_background_compact` method that compacts only when idle past a threshold AND context fill is above `LOW_THRESH`. The orchestrator background task (Task 5) just calls `maybe_background_compact`.

**Note on timestamps:** message documents are written with a `created_at` datetime elsewhere in the pipeline. `last_activity_seconds` reads the newest message's `created_at`; if absent it treats the session as not-idle (returns `0.0`) so a missing field never triggers a surprise compaction.

**Files:**
- Modify: `/Users/zachstallbohm/Work/Labmate/services/memory/context_manager.py`
- Test: `/Users/zachstallbohm/Work/Labmate/services/memory/tests/test_context_manager.py`

### Steps

- [ ] Add `datetime` to the imports. Find the top import block:

```python
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
```

Replace with:

```python
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
```

- [ ] Add the proactive-compaction constants next to the other compaction constants. Find (now including the constant added in Task 1):

```python
    _MICRO_STRIP_THRESHOLD = 1500   # chars; strip old message content beyond this
    _TOOL_RESULT_THRESHOLD = 600    # chars; tool results stripped more aggressively
    _BLOCK_SIZE            = 20     # turns per parallel summarization block
    _KEEP_RECENT           = 15     # turns retained verbatim after full compact
    _KEEP_RECENT_TOOL_RESULTS = 10  # most-recent tool results never cleared (still referenced)
```

Replace with:

```python
    _MICRO_STRIP_THRESHOLD = 1500   # chars; strip old message content beyond this
    _TOOL_RESULT_THRESHOLD = 600    # chars; tool results stripped more aggressively
    _BLOCK_SIZE            = 20     # turns per parallel summarization block
    _KEEP_RECENT           = 15     # turns retained verbatim after full compact
    _KEEP_RECENT_TOOL_RESULTS = 10  # most-recent tool results never cleared (still referenced)
    _IDLE_COMPACT_SECONDS  = 600    # idle threshold (s) before proactive background compact
    _LOW_FILL_RATIO        = 0.50   # only background-compact when fill ratio exceeds this
```

- [ ] Add `last_activity_seconds` and `maybe_background_compact` methods to `ContextManager`. Insert them immediately after `full_compact` (before the `# ── Consolidation worker ──` divider):

```python
    async def last_activity_seconds(self, session_id: str) -> float:
        """Seconds since the newest message in this session was written.

        Reads the newest message's created_at. Returns 0.0 when the session has
        no messages or the newest message lacks created_at — i.e. "not idle", so a
        missing timestamp never triggers a surprise background compaction.
        """
        cursor = (
            self.db["messages"]
            .find({"session_id": session_id}, {"created_at": 1})
            .sort("seq", -1)
            .limit(1)
        )
        newest = None
        async for doc in cursor:
            newest = doc.get("created_at")
            break
        if not isinstance(newest, datetime):
            return 0.0
        if newest.tzinfo is None:
            newest = newest.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - newest).total_seconds())

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
                session_id, idle, ctx.total_tokens, self.budget.effective_budget,
            )
            return await self.full_compact(session_id, llm_fn)
        except Exception:
            _logger.warning("background compact failed (non-fatal)", exc_info=True)
            return None
```

- [ ] Add tests. Append to `/Users/zachstallbohm/Work/Labmate/services/memory/tests/test_context_manager.py`:

```python
@pytest.mark.asyncio
async def test_last_activity_seconds_reports_idle_time():
    """last_activity_seconds returns roughly the age of the newest message."""
    import datetime as _dt
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        old_ts = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=900)
        db = MagicMock()
        cursor = _AsyncIter([{"created_at": old_ts}])
        mock_sort = MagicMock()
        mock_sort.limit = MagicMock(return_value=cursor)
        mock_find = MagicMock()
        mock_find.sort = MagicMock(return_value=mock_sort)
        messages_col = MagicMock()
        messages_col.find = MagicMock(return_value=mock_find)
        db.__getitem__ = MagicMock(return_value=messages_col)

        cm = ContextManager(redis=AsyncMock(), mongo_db=db, chroma_cols={}, embedder=AsyncMock())
        idle = await cm.last_activity_seconds("s1")
        assert idle >= 800   # ~900s, allow generous slack


@pytest.mark.asyncio
async def test_maybe_background_compact_skips_when_not_idle():
    """A recently-active session is never background-compacted."""
    import datetime as _dt
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        recent_ts = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=5)
        db = MagicMock()
        cursor = _AsyncIter([{"created_at": recent_ts}])
        mock_sort = MagicMock()
        mock_sort.limit = MagicMock(return_value=cursor)
        mock_find = MagicMock()
        mock_find.sort = MagicMock(return_value=mock_sort)
        messages_col = MagicMock()
        messages_col.find = MagicMock(return_value=mock_find)
        db.__getitem__ = MagicMock(return_value=messages_col)

        llm = AsyncMock()
        cm = ContextManager(redis=AsyncMock(), mongo_db=db, chroma_cols={}, embedder=AsyncMock())
        result = await cm.maybe_background_compact("s1", llm)
        assert result is None
        llm.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_background_compact_runs_when_idle_and_full():
    """An idle, high-fill session triggers full_compact."""
    import datetime as _dt
    with (
        patch("services.memory.context_manager.token_count", side_effect=_mock_token_count),
        patch("services.memory.context_manager.rerank", new_callable=AsyncMock, return_value=[]),
    ):
        from services.memory.context_manager import ContextManager, ContextBudget

        old_ts = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=1200)

        cm = ContextManager(
            redis=AsyncMock(), mongo_db=MagicMock(), chroma_cols={},
            embedder=AsyncMock(), budget=ContextBudget(max_tokens=400, completion_reserve=20),
        )
        cm.last_activity_seconds = AsyncMock(return_value=1200.0)

        from services.memory.context_manager import AssembledContext
        cm.build_context = AsyncMock(return_value=AssembledContext(total_tokens=10_000))
        cm.full_compact = AsyncMock(return_value={
            "summary_tokens": 50, "pruned_messages": 7, "reflections": [],
        })

        llm = AsyncMock()
        result = await cm.maybe_background_compact("s1", llm)
        assert result is not None
        assert result["pruned_messages"] == 7
        cm.full_compact.assert_awaited_once()
        # old_ts retained to document the idle scenario under test
        assert old_ts < _dt.datetime.now(_dt.timezone.utc)
```

- [ ] Run the tests:

```bash
cd /Users/zachstallbohm/Work/Labmate && python -m pytest services/memory/tests/test_context_manager.py -x -q
```

- [ ] Commit:

```bash
cd /Users/zachstallbohm/Work/Labmate && git add services/memory/context_manager.py services/memory/tests/test_context_manager.py && git commit -m "feat(memory): idle detection + maybe_background_compact gate

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 5 — Background compaction sweeper task in the orchestrator

**Why:** Wire `maybe_background_compact` into a non-blocking asyncio background task. Every sweep interval it scans sessions with recent messages and proactively compacts the idle, high-fill ones. It runs alongside the goal loop, never blocks message handling, and is cancelled cleanly on shutdown — mirroring the existing OutboxWorker lifecycle pattern in `StorageManager.__aenter__/__aexit__`.

The sweeper finds candidate sessions via the `messages` collection's distinct `session_id` values, then defers all gating to `maybe_background_compact` (idle + fill gates live there). Reflections produced by a background compact are written via `storage.consolidator.write_reflections` in a fire-and-forget task, exactly like the manual/auto paths.

**Files:**
- Modify: `/Users/zachstallbohm/Work/Labmate/services/orchestrator/main.py`
- Test: `/Users/zachstallbohm/Work/Labmate/tests/services/orchestrator/test_background_compaction.py` (create)

### Steps

- [ ] Add the sweeper config constants under the existing threshold block in `main.py`. Find:

```python
CTX_TOKENS   = int(os.getenv("CTX_WINDOW", "131072"))
MICRO_THRESH = int(CTX_TOKENS * 0.70)
FULL_THRESH  = int(CTX_TOKENS * 0.85)
```

Replace with:

```python
CTX_TOKENS   = int(os.getenv("CTX_WINDOW", "131072"))
MICRO_THRESH = int(CTX_TOKENS * 0.70)
FULL_THRESH  = int(CTX_TOKENS * 0.85)

# Background proactive compaction: how often the sweeper wakes, and the cap on
# sessions inspected per sweep (newest-active first) so one sweep stays bounded.
BG_COMPACT_INTERVAL_S = int(os.getenv("BG_COMPACT_INTERVAL_S", "120"))
BG_COMPACT_MAX_SESSIONS = int(os.getenv("BG_COMPACT_MAX_SESSIONS", "20"))
```

- [ ] Add the sweeper method to `OrchestratorProcess`. Insert it immediately after `_loop` (before `_handle`):

```python
    async def _background_compactor(
        self,
        orch: CodingOrchestrator,
        storage: StorageManager,
    ) -> None:
        """Periodically compact idle, high-fill sessions so the next message has room.

        Runs as its own asyncio task next to the goal loop. Each gate (idle + fill)
        lives in ContextManager.maybe_background_compact; this method only finds
        candidate sessions and dispatches. Best-effort: one session's failure never
        stops the sweep, and the sweep never blocks goal handling.
        """
        async def _bg_llm(p: str) -> str:
            import litellm as _litellm
            r = await _litellm.acompletion(
                model="openai/gemma-4-31b",
                api_base=orch._gemma_base,
                api_key="not-needed",
                messages=[{"role": "user", "content": p}],
                extra_body={"thinking_budget_tokens": 0},
            )
            return r.choices[0].message.content

        while not self._shutdown.is_set():
            try:
                await asyncio.sleep(BG_COMPACT_INTERVAL_S)
                if self._shutdown.is_set():
                    break

                session_ids = await storage._db["messages"].distinct("session_id")
                for session_id in session_ids[:BG_COMPACT_MAX_SESSIONS]:
                    if self._shutdown.is_set():
                        break
                    if not session_id:
                        continue
                    result = await storage.context_manager.maybe_background_compact(
                        session_id, _bg_llm,
                    )
                    if result and result.get("reflections"):
                        asyncio.create_task(storage.consolidator.write_reflections(
                            session_id, result["reflections"]
                        ))
                    if result:
                        _log.info(
                            "background compact: session %s pruned %d messages",
                            session_id, result.get("pruned_messages", 0),
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.warning("background compactor sweep failed (non-fatal)", exc_info=True)
```

- [ ] Start the sweeper task alongside the goal loop, and cancel it on shutdown. Find:

```python
            orch.graph = graph

            _log.info("orchestrator %s ready", self._worker_id)
            await self._loop(orch, _sm)

        if self._mcp:
            await self._mcp.shutdown()
        if self._codegraph_mcp:
            await self._codegraph_mcp.shutdown()
```

Replace with:

```python
            orch.graph = graph

            _log.info("orchestrator %s ready", self._worker_id)

            bg_compactor = asyncio.create_task(
                self._background_compactor(orch, _sm), name="background-compactor",
            )
            try:
                await self._loop(orch, _sm)
            finally:
                bg_compactor.cancel()
                try:
                    await bg_compactor
                except asyncio.CancelledError:
                    pass

        if self._mcp:
            await self._mcp.shutdown()
        if self._codegraph_mcp:
            await self._codegraph_mcp.shutdown()
```

- [ ] Create the integration test `/Users/zachstallbohm/Work/Labmate/tests/services/orchestrator/test_background_compaction.py`:

```python
"""Integration tests for the orchestrator background compaction sweeper."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_background_compactor_compacts_idle_session(monkeypatch):
    """A session idle past the threshold with high fill triggers a background compact
    and its reflections are written to memory."""
    import services.orchestrator.main as main

    # Wake the sweeper almost immediately and inspect a small batch.
    monkeypatch.setattr(main, "BG_COMPACT_INTERVAL_S", 0)
    monkeypatch.setattr(main, "BG_COMPACT_MAX_SESSIONS", 20)

    proc = main.OrchestratorProcess()

    # orch only needs a _gemma_base attribute for the bg llm closure.
    orch = MagicMock()
    orch._gemma_base = "http://localhost:8000/v1"

    # context_manager.maybe_background_compact returns a compact result with reflections.
    context_manager = MagicMock()
    context_manager.maybe_background_compact = AsyncMock(return_value={
        "summary_tokens": 40,
        "pruned_messages": 6,
        "reflections": ["use Redis streams"],
    })

    consolidator = MagicMock()
    consolidator.write_reflections = AsyncMock()

    storage = MagicMock()
    storage.context_manager = context_manager
    storage.consolidator = consolidator
    # storage._db["messages"].distinct(...) → one candidate session.
    messages_col = MagicMock()
    messages_col.distinct = AsyncMock(return_value=["sess-1"])
    storage._db = {"messages": messages_col}

    # Run one sweep, then signal shutdown so the loop exits.
    task = asyncio.create_task(proc._background_compactor(orch, storage))
    # Give the sweeper time to perform at least one iteration.
    for _ in range(50):
        await asyncio.sleep(0)
        if context_manager.maybe_background_compact.await_count:
            break
    proc._shutdown.set()
    await asyncio.wait_for(task, timeout=2.0)

    context_manager.maybe_background_compact.assert_awaited()
    assert context_manager.maybe_background_compact.await_args[0][0] == "sess-1"
    # Reflections written fire-and-forget; let the created task run.
    await asyncio.sleep(0)
    consolidator.write_reflections.assert_awaited_with("sess-1", ["use Redis streams"])


@pytest.mark.asyncio
async def test_background_compactor_skips_when_maybe_returns_none(monkeypatch):
    """When maybe_background_compact returns None (not idle / low fill), no reflections
    are written and the sweep continues without error."""
    import services.orchestrator.main as main

    monkeypatch.setattr(main, "BG_COMPACT_INTERVAL_S", 0)
    monkeypatch.setattr(main, "BG_COMPACT_MAX_SESSIONS", 20)

    proc = main.OrchestratorProcess()
    orch = MagicMock()
    orch._gemma_base = "http://localhost:8000/v1"

    context_manager = MagicMock()
    context_manager.maybe_background_compact = AsyncMock(return_value=None)

    consolidator = MagicMock()
    consolidator.write_reflections = AsyncMock()

    storage = MagicMock()
    storage.context_manager = context_manager
    storage.consolidator = consolidator
    messages_col = MagicMock()
    messages_col.distinct = AsyncMock(return_value=["sess-1"])
    storage._db = {"messages": messages_col}

    task = asyncio.create_task(proc._background_compactor(orch, storage))
    for _ in range(50):
        await asyncio.sleep(0)
        if context_manager.maybe_background_compact.await_count:
            break
    proc._shutdown.set()
    await asyncio.wait_for(task, timeout=2.0)

    context_manager.maybe_background_compact.assert_awaited()
    consolidator.write_reflections.assert_not_awaited()
```

- [ ] Run the orchestrator integration tests:

```bash
cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/orchestrator/test_background_compaction.py -x -q
```

- [ ] Run the full memory + orchestrator compaction suites to confirm no regressions:

```bash
cd /Users/zachstallbohm/Work/Labmate && python -m pytest services/memory/tests/test_context_manager.py tests/services/orchestrator/test_background_compaction.py -q
```

- [ ] Commit:

```bash
cd /Users/zachstallbohm/Work/Labmate && git add services/orchestrator/main.py tests/services/orchestrator/test_background_compaction.py && git commit -m "feat(orchestrator): background sweeper for proactive idle-session compaction

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Final verification

- [ ] Run the complete compaction-related test suite one final time:

```bash
cd /Users/zachstallbohm/Work/Labmate && python -m pytest services/memory/tests/test_context_manager.py tests/services/orchestrator/test_background_compaction.py -q
```

- [ ] Confirm no service writes to stdout (logging-to-stderr rule): the new code uses `_logger` / `_log` only. Grep to confirm no stray prints were introduced:

```bash
cd /Users/zachstallbohm/Work/Labmate && grep -rn "print(" services/memory/context_manager.py services/orchestrator/main.py
```

Expect no matches.

- [ ] Confirm every litellm call added in this plan passes `api_key="not-needed"` and `extra_body={"thinking_budget_tokens": ...}` (the `_bg_llm` closure in Task 5). Visually verify against `main.py`.

---

## Gherkin Scenarios

These scenarios describe expected system behavior at the feature level. Use them for acceptance testing and to verify the implementation matches intent.

```gherkin
Feature: Age-aware tool result clearing

  Scenario: Old tool results are stripped during compaction
    Given a session with 15 tool result messages each containing over 600 characters
    And the 10 most recent tool results are still in-use
    When clear_tool_results is called for the session
    Then the 5 oldest tool results are replaced with stubs
    And the 10 most recent tool results are unchanged
    And the returned freed-chars count is positive

  Scenario: Session with fewer than 10 tool results clears nothing
    Given a session with 8 tool result messages each containing over 600 characters
    When clear_tool_results is called
    Then no messages are modified
    And 0 is returned

  Scenario: Motor query receives the correct skip offset
    Given a session with tool result messages of varying ages
    When clear_tool_results is called
    Then the MongoDB cursor receives .skip(10)
    And the cursor is sorted by seq descending


Feature: Anchor surfacing in context assembly

  Scenario: Diverged anchor is prepended to assembled context
    Given a session with an anchor containing "Gemma model Redis streams goals"
    And the current rolling summary contains only "weather topics discussed today"
    When build_context is called
    Then the assembled prompt contains "KEY FACTS"
    And the anchor buffer contains "Gemma"
    And the anchor section appears after core memory and before RAG

  Scenario: Anchor is omitted when already covered by the summary
    Given a session with an anchor "project uses Gemma model Redis streams"
    And a summary that also contains "Gemma model Redis streams"
    When build_context is called
    Then the anchor buffer is empty
    And "KEY FACTS" does not appear in the prompt

  Scenario: Missing anchor key returns empty anchor buffer
    Given a session with no anchor key in Redis
    When build_context is called
    Then the anchor buffer is empty

  Scenario: Context ordering is core → anchor → RAG → summary → recent
    Given a session with agent instructions, core memory, anchor, RAG, summary, and recent turns
    When as_prompt is called on the assembled context
    Then core memory appears before the anchor buffer
    And the anchor buffer appears before RAG
    And RAG appears before the summary
    And the summary appears before recent turns


Feature: Compaction quality event emission

  Scenario: full_compact emits a compact.quality event
    Given a session with 20 conversation turns
    When full_compact is called with a valid LLM function
    Then a compact.quality event is emitted
    And the event payload contains compression_ratio
    And the event payload contains turns_compacted
    And the event payload contains tokens_saved
    And the event payload contains reflections_count
    And the event payload contains session_id

  Scenario: Emitter failure does not change the return value
    Given the events.emit function raises an exception
    When full_compact completes
    Then the returned dict still contains summary_tokens
    And the returned dict still contains pruned_messages
    And the returned dict still contains reflections

  Scenario: Event is a no-op when no task emitter is set
    Given no ContextVar emitter is set in the current task scope
    When full_compact is called
    Then no exception is raised
    And the compact result is returned normally


Feature: Idle session detection

  Scenario: Idle time is reported from the newest message timestamp
    Given a session whose most recent message was written 900 seconds ago
    When last_activity_seconds is called
    Then a value of approximately 900 is returned

  Scenario: Session with no messages reports not-idle
    Given a session with no messages in the database
    When last_activity_seconds is called
    Then 0.0 is returned

  Scenario: Session with missing created_at on newest message reports not-idle
    Given a session whose newest message document has no created_at field
    When last_activity_seconds is called
    Then 0.0 is returned

  Scenario: Recently active session is not background-compacted
    Given a session whose most recent message was written 5 seconds ago
    When maybe_background_compact is called
    Then None is returned
    And the LLM function is never called

  Scenario: Idle high-fill session triggers proactive compaction
    Given a session idle for 1200 seconds (exceeding the 600 s threshold)
    And the session context fill token count exceeds the LOW_FILL_RATIO threshold
    When maybe_background_compact is called
    Then full_compact is called
    And the compact result dict is returned

  Scenario: Idle low-fill session is not compacted
    Given a session idle for 1200 seconds
    And the session context fill is below the LOW_FILL_RATIO threshold
    When maybe_background_compact is called
    Then None is returned


Feature: Orchestrator background compaction sweeper

  Scenario: Sweeper compacts eligible idle sessions on each sweep
    Given an orchestrator with a running background compactor task
    And session "sess-1" qualifies for background compaction
    When one sweep cycle completes
    Then maybe_background_compact is called with "sess-1"
    And write_reflections is called with the returned reflections

  Scenario: Sweeper skips sessions that do not qualify
    Given an orchestrator with a running background compactor task
    And session "sess-1" returns None from maybe_background_compact
    When one sweep cycle completes
    Then write_reflections is not called

  Scenario: Sweeper is cancelled cleanly when the orchestrator shuts down
    Given a running background compactor task
    When the orchestrator goal loop exits
    Then the background compactor task is cancelled
    And the cancellation is awaited without raising

  Scenario: Sweeper sweep failure does not crash the orchestrator
    Given maybe_background_compact raises an unexpected exception
    When the sweeper catches the exception
    Then the sweeper logs a warning
    And continues to the next sweep interval
```
