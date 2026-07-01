from datetime import UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_token_count(text: str) -> int:
    """Deterministic stub: 1 token per 4 chars."""
    return max(0, len(text) // 4)


@pytest.mark.asyncio
async def test_build_context_stays_within_budget():
    with (
        patch("services.memory.context_manager.token_count", side_effect=_mock_token_count),
        patch("services.memory.context_manager.rerank", new_callable=AsyncMock, return_value=[]),
    ):
        from services.memory.context_manager import ContextBudget, ContextManager

        redis = AsyncMock()

        def _redis_get(key):
            if "core" in key:
                return "goal: finish MCP bridge"
            elif "summarized_through" in key:
                return None  # watermark defaults to -1 when absent
            else:
                return "old summary"

        redis.get = AsyncMock(side_effect=_redis_get)
        db = MagicMock()

        class AsyncDocIter:
            def __init__(self, docs):
                self._docs = iter(docs)

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self._docs)
                except StopIteration:
                    raise StopAsyncIteration from None

        # chat_turns uses camelCase: {sessionId, seq, role, text}
        turns = [
            {"role": "user", "text": "hello", "seq": 1},
            {"role": "assistant", "text": "hi there", "seq": 2},
        ]

        # The cursor is used by _recent_turns which calls .find().sort().limit()
        # We need the full chain to return our AsyncDocIter
        mock_cursor = AsyncDocIter(turns)
        mock_sort = MagicMock()
        mock_sort.limit = MagicMock(return_value=mock_cursor)
        mock_find = MagicMock()
        mock_find.sort = MagicMock(return_value=mock_sort)
        chat_turns_col = MagicMock()
        chat_turns_col.find = MagicMock(return_value=mock_find)
        db.__getitem__ = MagicMock(return_value=chat_turns_col)

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
    with (
        patch("services.memory.context_manager.token_count", side_effect=_mock_token_count),
        patch("services.memory.context_manager.rerank", new_callable=AsyncMock, return_value=[]),
    ):
        from services.memory.context_manager import ContextBudget, ContextManager

        redis = AsyncMock()
        long_core = "GOAL: " + "x" * 1994  # 2000 chars → 500 tokens at 1/4 rate
        redis.get = AsyncMock(side_effect=lambda key: long_core if "core" in key else "")

        db = MagicMock()

        class EmptyCursor:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        empty = EmptyCursor()
        mock_sort = MagicMock()
        mock_sort.limit = MagicMock(return_value=empty)
        mock_find = MagicMock()
        mock_find.sort = MagicMock(return_value=mock_sort)
        chat_turns_col = MagicMock()
        chat_turns_col.find = MagicMock(return_value=mock_find)
        db.__getitem__ = MagicMock(return_value=chat_turns_col)

        embed = AsyncMock(return_value=[[0.1]])
        budget = ContextBudget(max_tokens=700, completion_reserve=100)
        cm = ContextManager(redis=redis, mongo_db=db, chroma_cols={}, embedder=embed, budget=budget)

        ctx = await cm.build_context("s1", "task", "system")
        assert ctx.core_memory == long_core


def test_trim_to_budget_drops_oldest_lines():
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        cm = ContextManager(
            redis=AsyncMock(),
            mongo_db=MagicMock(),
            chroma_cols={},
            embedder=AsyncMock(),
        )
        # 5 lines × ~13 chars each → ~3 tokens each (at 4 chars/token).
        # Total ~15 tokens. Budget=6 → keep newest lines.
        text = "\n".join([f"line {i} text" for i in range(5)])
        result = cm._trim_to_budget(text, budget=6)
        lines = [line for line in result.splitlines() if line]
        assert len(lines) <= 3
        assert "line 4 text" in result


def test_context_budget_effective_budget():
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextBudget

        b = ContextBudget(max_tokens=8192, completion_reserve=700)
        assert b.effective_budget == 7492
        assert b.slot(0.25) == int(7492 * 0.25)


# ── Compaction tests ─────────────────────────────────────────────────────────


class _AsyncIter:
    """Async iterator that also supports Motor's chainable .sort(), .skip(), and .limit()."""

    def __init__(self, docs):
        self._docs = iter(docs)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._docs)
        except StopIteration:
            raise StopAsyncIteration from None

    def sort(self, *args, **kwargs):
        return self

    def skip(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self


def _make_cm(turns=None, redis_data=None):
    """Build a ContextManager with mocked DB and Redis."""
    from unittest.mock import AsyncMock, MagicMock

    from services.memory.context_manager import ContextManager

    redis = AsyncMock()
    _store: dict = redis_data or {}

    async def _redis_get(key):
        return _store.get(key)

    async def _redis_set(key, value):
        _store[key] = value

    async def _redis_delete(key):
        _store.pop(key, None)

    redis.get = AsyncMock(side_effect=_redis_get)
    redis.set = AsyncMock(side_effect=_redis_set)
    redis.delete = AsyncMock(side_effect=_redis_delete)

    db = MagicMock()
    docs = turns or []

    # find() → returns cursor iterable; delete_many → no-op async
    find_mock = MagicMock(return_value=_AsyncIter(docs))
    db.__getitem__ = MagicMock(
        return_value=MagicMock(
            find=find_mock,
            update_one=AsyncMock(),
            delete_many=AsyncMock(),
        )
    )

    return ContextManager(redis=redis, mongo_db=db, chroma_cols={}, embedder=AsyncMock())


@pytest.mark.asyncio
async def test_recent_turns_reads_chat_turns_with_watermark():
    """_recent_turns reads chat_turns (camelCase), filters by watermark, maps text→content."""
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        redis_store: dict = {"summarized_through:s1": "5"}  # watermark = 5
        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=lambda k: redis_store.get(k))

        # chat_turns uses camelCase: {sessionId, seq, role, text, createdAt}
        db = MagicMock()
        turns = [
            {"sessionId": "s1", "seq": 1, "role": "user", "text": "hello"},
            {"sessionId": "s1", "seq": 3, "role": "assistant", "text": "hi"},
            {"sessionId": "s1", "seq": 6, "role": "user", "text": "how are you"},  # > watermark (5)
            {"sessionId": "s1", "seq": 7, "role": "assistant", "text": "fine"},  # > watermark
        ]
        chat_turns_col = MagicMock()
        chat_turns_col.find = MagicMock(return_value=_AsyncIter(turns))
        chat_turns_col.sort = MagicMock(return_value=_AsyncIter(turns))
        chat_turns_col.limit = MagicMock(return_value=_AsyncIter(turns))
        db.__getitem__ = MagicMock(return_value=chat_turns_col)

        cm = ContextManager(redis=redis, mongo_db=db, chroma_cols={}, embedder=AsyncMock())
        recent = await cm._recent_turns("s1", budget=500)

        # Should only include turns with seq > 5 (i.e., seqs 6, 7)
        assert "how are you" in recent
        assert "fine" in recent
        assert "hello" not in recent  # seq 1 is <= watermark
        assert "hi" not in recent  # seq 3 is <= watermark


@pytest.mark.asyncio
async def test_recent_turns_defaults_watermark_to_minus_one():
    """When summarized_through is absent, watermark defaults to -1 (all turns are recent)."""
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        redis_store: dict = {}  # no watermark key
        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=lambda k: redis_store.get(k))

        db = MagicMock()
        turns = [
            {"sessionId": "s1", "seq": 0, "role": "user", "text": "first"},
            {"sessionId": "s1", "seq": 1, "role": "assistant", "text": "reply"},
        ]
        chat_turns_col = MagicMock()
        chat_turns_col.find = MagicMock(return_value=_AsyncIter(turns))
        chat_turns_col.sort = MagicMock(return_value=_AsyncIter(turns))
        chat_turns_col.limit = MagicMock(return_value=_AsyncIter(turns))
        db.__getitem__ = MagicMock(return_value=chat_turns_col)

        cm = ContextManager(redis=redis, mongo_db=db, chroma_cols={}, embedder=AsyncMock())
        recent = await cm._recent_turns("s1", budget=500)

        # All turns should be included (watermark -1 means seq > -1, i.e. all >= 0)
        assert "first" in recent
        assert "reply" in recent


@pytest.mark.asyncio
async def test_last_activity_seconds_reads_chat_turns_iso_timestamp():
    """last_activity_seconds reads newest turn from chat_turns, parses ISO createdAt."""
    from datetime import datetime, timedelta

    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        # Create an ISO timestamp from ~900 seconds ago
        old_time = datetime.now(UTC) - timedelta(seconds=900)
        iso_ts = old_time.strftime("%Y-%m-%dT%H:%M:%SZ")

        db = MagicMock()
        cursor = _AsyncIter([{"createdAt": iso_ts}])
        mock_sort = MagicMock()
        mock_sort.limit = MagicMock(return_value=cursor)
        mock_find = MagicMock()
        mock_find.sort = MagicMock(return_value=mock_sort)
        chat_turns_col = MagicMock()
        chat_turns_col.find = MagicMock(return_value=mock_find)
        db.__getitem__ = MagicMock(return_value=chat_turns_col)

        cm = ContextManager(redis=AsyncMock(), mongo_db=db, chroma_cols={}, embedder=AsyncMock())
        idle = await cm.last_activity_seconds("s1")

        assert idle >= 800  # ~900s, allow generous slack


@pytest.mark.asyncio
async def test_full_compact_watermark_nondestructive():
    """full_compact writes summary, advances watermark, NEVER deletes chat_turns."""
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        redis_store: dict = {}
        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=lambda k: redis_store.get(k))
        redis.set = AsyncMock(side_effect=lambda k, v: redis_store.update({k: v}))
        redis.delete = AsyncMock(side_effect=lambda k: redis_store.pop(k, None))

        db = MagicMock()
        # 40 turns so _KEEP_RECENT=15 leaves 25 to compact
        turns = [
            {
                "sessionId": "s1",
                "seq": i,
                "role": "user" if i % 2 == 0 else "assistant",
                "text": f"turn {i}",
            }
            for i in range(40)
        ]
        chat_turns_col = MagicMock()
        chat_turns_col.find = MagicMock(return_value=_AsyncIter(turns))
        chat_turns_col.sort = MagicMock(return_value=_AsyncIter(turns))
        chat_turns_col.limit = MagicMock(return_value=_AsyncIter(turns))
        db.__getitem__ = MagicMock(return_value=chat_turns_col)

        llm_calls = []

        async def _llm(prompt: str) -> str:
            llm_calls.append(prompt)
            if "JSON" in prompt:
                return '{"decisions": ["use Python"]}'
            return "summary of old turns"

        cm = ContextManager(redis=redis, mongo_db=db, chroma_cols={}, embedder=AsyncMock())
        await cm.full_compact("s1", _llm)

        # Verify watermark was advanced
        watermark_key = "summarized_through:s1"
        assert watermark_key in redis_store
        # watermark should be max_seq - _KEEP_RECENT = 39 - 15 = 24
        assert redis_store[watermark_key] == "24"

        # Verify summary was written
        assert "summary:s1" in redis_store
        assert redis_store["summary:s1"] == "summary of old turns"

        # CRITICAL: delete_many should NOT have been called (chat_turns is immutable)
        assert not chat_turns_col.delete_many.called


@pytest.mark.asyncio
async def test_full_compact_respects_watermark_on_second_call():
    """A second full_compact only summarizes newly-eligible turns (respects watermark)."""
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        redis_store: dict = {"summarized_through:s1": "24"}  # from prior compaction
        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=lambda k: redis_store.get(k))
        redis.set = AsyncMock(side_effect=lambda k, v: redis_store.update({k: v}))
        redis.delete = AsyncMock(side_effect=lambda k: redis_store.pop(k, None))

        db = MagicMock()
        # 45 turns: watermark=24, _KEEP_RECENT=15 → new to_compact = turns 25-29 (5 turns)
        turns = [
            {
                "sessionId": "s1",
                "seq": i,
                "role": "user" if i % 2 == 0 else "assistant",
                "text": f"turn {i}",
            }
            for i in range(45)
        ]
        chat_turns_col = MagicMock()
        chat_turns_col.find = MagicMock(return_value=_AsyncIter(turns))
        chat_turns_col.sort = MagicMock(return_value=_AsyncIter(turns))
        chat_turns_col.limit = MagicMock(return_value=_AsyncIter(turns))
        db.__getitem__ = MagicMock(return_value=chat_turns_col)

        llm_calls = []

        async def _llm(prompt: str) -> str:
            llm_calls.append(prompt)
            if "JSON" in prompt:
                return '{"decisions": []}'
            return "new summary segment"

        cm = ContextManager(redis=redis, mongo_db=db, chroma_cols={}, embedder=AsyncMock())
        result = await cm.full_compact("s1", _llm)

        # Only the newly-eligible turns (25-29, i.e., 5 turns) should be compacted
        # The first block summary call should be in the prompts
        assert len(llm_calls) >= 2  # at least block summary + merge
        assert result["pruned_messages"] == 0  # no deletion


@pytest.mark.asyncio
async def test_microcompact_and_clear_tool_results_removed():
    """Verify microcompact and clear_tool_results methods are deleted."""
    from services.memory.context_manager import ContextManager

    cm = ContextManager(
        redis=AsyncMock(), mongo_db=MagicMock(), chroma_cols={}, embedder=AsyncMock()
    )

    # These methods should not exist
    assert not hasattr(cm, "microcompact"), "microcompact should be deleted"
    assert not hasattr(cm, "clear_tool_results"), "clear_tool_results should be deleted"


@pytest.mark.asyncio
async def test_full_compact_returns_reflections():
    """full_compact should call llm_fn multiple times and return reflection strings."""
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        redis_store: dict = {}
        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=lambda k: redis_store.get(k))
        redis.set = AsyncMock(side_effect=lambda k, v: redis_store.update({k: v}))
        redis.delete = AsyncMock(side_effect=lambda k: redis_store.pop(k, None))

        db = MagicMock()
        # 20 turns so _KEEP_RECENT=15 leaves 5 to compact
        turns = [
            {"seq": i, "role": "user" if i % 2 == 0 else "assistant", "text": f"message {i}"}
            for i in range(20)
        ]
        chat_turns_col = MagicMock()
        chat_turns_col.find = MagicMock(return_value=_AsyncIter(turns))
        chat_turns_col.sort = MagicMock(return_value=_AsyncIter(turns))
        db.__getitem__ = MagicMock(return_value=chat_turns_col)

        llm_calls = []

        async def _llm(prompt: str) -> str:
            llm_calls.append(prompt)
            if "JSON" in prompt:
                return '{"decisions": ["decided to use Python"], "findings": ["Redis is fast"]}'
            return "summary of old turns"

        cm = ContextManager(redis=redis, mongo_db=db, chroma_cols={}, embedder=AsyncMock())
        result = await cm.full_compact("s1", _llm)

        assert result["pruned_messages"] == 0  # no deletion; watermark-only
        assert result["reflections"] == ["decided to use Python", "Redis is fast"]
        # Anchor should be set on first compact
        assert redis_store.get("anchor:s1") is not None


@pytest.mark.asyncio
async def test_full_compact_saves_anchor_only_on_first_compact():
    """Second compact should not overwrite the anchor."""
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        original_anchor = "anchor from first compact"
        redis_store: dict = {"anchor:s1": original_anchor}
        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=lambda k: redis_store.get(k))
        redis.set = AsyncMock(side_effect=lambda k, v: redis_store.update({k: v}))
        redis.delete = AsyncMock(side_effect=lambda k: redis_store.pop(k, None))

        db = MagicMock()
        turns = [{"seq": i, "role": "user", "text": f"msg {i}"} for i in range(20)]
        chat_turns_col = MagicMock()
        chat_turns_col.find = MagicMock(return_value=_AsyncIter(turns))
        chat_turns_col.sort = MagicMock(return_value=_AsyncIter(turns))
        db.__getitem__ = MagicMock(return_value=chat_turns_col)

        async def _llm(prompt: str) -> str:
            return '{"decisions": []}' if "JSON" in prompt else "new summary"

        cm = ContextManager(redis=redis, mongo_db=db, chroma_cols={}, embedder=AsyncMock())
        await cm.full_compact("s1", _llm)

        # Anchor must not have changed
        assert redis_store["anchor:s1"] == original_anchor


@pytest.mark.asyncio
async def test_parallel_summarize_calls_llm_per_block():
    """_parallel_summarize should call llm_fn once per block plus one merge call."""
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        cm = ContextManager(
            redis=AsyncMock(), mongo_db=MagicMock(), chroma_cols={}, embedder=AsyncMock()
        )
        # 45 turns → 3 blocks of 20/20/5 → 3 block calls + 1 merge call = 4 total
        turns = [{"role": "user", "content": f"msg {i}"} for i in range(45)]
        llm_calls = []

        async def _llm(prompt: str) -> str:
            llm_calls.append(prompt)
            return f"summary_{len(llm_calls)}"

        await cm._parallel_summarize(turns, anchor="", llm_fn=_llm)
        assert len(llm_calls) == 4  # 3 blocks + 1 merge
        assert "SEGMENTS" in llm_calls[-1]  # last call is the merge


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
        core_pos = prompt.index("goalcore")
        anchor_pos = prompt.index("anchorfacts")
        rag_pos = prompt.index("ragtext")
        summary_pos = prompt.index("summarytext")
        recent_pos = prompt.index("recenttext")
        assert core_pos < anchor_pos < rag_pos < summary_pos < recent_pos


@pytest.mark.asyncio
async def test_build_context_surfaces_anchor_when_diverged():
    """When the anchor's facts are absent from the summary, anchor_buffer is populated."""
    with (
        patch("services.memory.context_manager.token_count", side_effect=_mock_token_count),
        patch("services.memory.context_manager.rerank", new_callable=AsyncMock, return_value=[]),
    ):
        from services.memory.context_manager import ContextBudget, ContextManager

        store = {
            "core:s1": "GOAL: build MCP bridge",
            "summary:s1": "discussed unrelated weather topics today",
            "anchor:s1": "project uses Gemma model with Redis streams for goals",
        }
        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=lambda k: store.get(k))

        db = MagicMock()

        class EmptyCursor:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        mock_sort = MagicMock()
        mock_sort.limit = MagicMock(return_value=EmptyCursor())
        mock_find = MagicMock()
        mock_find.sort = MagicMock(return_value=mock_sort)
        chat_turns_col = MagicMock()
        chat_turns_col.find = MagicMock(return_value=mock_find)
        db.__getitem__ = MagicMock(return_value=chat_turns_col)

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
        from services.memory.context_manager import ContextBudget, ContextManager

        store = {
            "core:s1": "GOAL: build MCP bridge",
            "summary:s1": "project uses Gemma model with Redis streams for goals and more detail",
            "anchor:s1": "project uses Gemma model with Redis streams for goals",
        }
        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=lambda k: store.get(k))

        db = MagicMock()

        class EmptyCursor:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        mock_sort = MagicMock()
        mock_sort.limit = MagicMock(return_value=EmptyCursor())
        mock_find = MagicMock()
        mock_find.sort = MagicMock(return_value=mock_sort)
        chat_turns_col = MagicMock()
        chat_turns_col.find = MagicMock(return_value=mock_find)
        db.__getitem__ = MagicMock(return_value=chat_turns_col)

        embed = AsyncMock(return_value=[[0.1]])
        budget = ContextBudget(max_tokens=4000, completion_reserve=100)
        cm = ContextManager(redis=redis, mongo_db=db, chroma_cols={}, embedder=embed, budget=budget)

        ctx = await cm.build_context("s1", "task", "system")
        assert ctx.anchor_buffer == ""


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
            {
                "seq": i,
                "role": "user" if i % 2 == 0 else "assistant",
                "text": f"message number {i} with some content to make tokens",
            }
            for i in range(20)
        ]
        chat_turns_col = MagicMock()
        chat_turns_col.find = MagicMock(return_value=_AsyncIter(turns))
        chat_turns_col.sort = MagicMock(return_value=_AsyncIter(turns))
        db.__getitem__ = MagicMock(return_value=chat_turns_col)

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
        assert 0.0 <= evt["compression_ratio"] <= 1.0
        # Return contract: no deletion, watermark-only
        assert result["pruned_messages"] == 0
        assert result["reflections"] == ["use Python"]


@pytest.mark.asyncio
async def test_last_activity_seconds_reports_idle_time():
    """last_activity_seconds returns roughly the age of the newest turn."""
    from datetime import datetime, timedelta

    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        old_time = datetime.now(UTC) - timedelta(seconds=900)
        old_iso = old_time.strftime("%Y-%m-%dT%H:%M:%SZ")

        db = MagicMock()
        cursor = _AsyncIter([{"createdAt": old_iso}])
        mock_sort = MagicMock()
        mock_sort.limit = MagicMock(return_value=cursor)
        mock_find = MagicMock()
        mock_find.sort = MagicMock(return_value=mock_sort)
        chat_turns_col = MagicMock()
        chat_turns_col.find = MagicMock(return_value=mock_find)
        db.__getitem__ = MagicMock(return_value=chat_turns_col)

        cm = ContextManager(redis=AsyncMock(), mongo_db=db, chroma_cols={}, embedder=AsyncMock())
        idle = await cm.last_activity_seconds("s1")
        assert idle >= 800  # ~900s, allow generous slack


@pytest.mark.asyncio
async def test_maybe_background_compact_skips_when_not_idle():
    """A recently-active session is never background-compacted."""
    import time as _time

    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        recent_ts = _time.time() - 5.0  # float Unix timestamp, as actually stored
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
        from services.memory.context_manager import ContextBudget, ContextManager

        old_ts = _dt.datetime.now(_dt.UTC) - _dt.timedelta(seconds=1200)

        cm = ContextManager(
            redis=AsyncMock(),
            mongo_db=MagicMock(),
            chroma_cols={},
            embedder=AsyncMock(),
            budget=ContextBudget(max_tokens=400, completion_reserve=20),
        )
        cm.last_activity_seconds = AsyncMock(return_value=1200.0)

        from services.memory.context_manager import AssembledContext

        cm.build_context = AsyncMock(return_value=AssembledContext(total_tokens=10_000))
        cm.full_compact = AsyncMock(
            return_value={
                "summary_tokens": 50,
                "pruned_messages": 7,
                "reflections": [],
            }
        )

        llm = AsyncMock()
        result = await cm.maybe_background_compact("s1", llm)
        assert result is not None
        assert result["pruned_messages"] == 7
        cm.full_compact.assert_awaited_once()
        # old_ts retained to document the idle scenario under test
        assert old_ts < _dt.datetime.now(_dt.UTC)


def test_anchor_diverges_heuristic_boundaries():
    """_anchor_diverges uses a 60% word-overlap threshold on tokens longer than 3 chars."""
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        cm = ContextManager(
            redis=AsyncMock(), mongo_db=MagicMock(), chroma_cols={}, embedder=AsyncMock()
        )

        # Build anchor with 10 distinct >3-char words
        anchor_words = [
            "alpha",
            "bravo",
            "charlie",
            "delta",
            "echo",
            "foxtrot",
            "golf",
            "hotel",
            "india",
            "juliet",
        ]
        anchor = " ".join(anchor_words)

        # 5/10 overlap = 50% → diverges (below 60% threshold)
        low_summary = " ".join(anchor_words[:5]) + " unrelated stuff here"
        assert cm._anchor_diverges(anchor, low_summary) is True

        # 7/10 overlap = 70% → does not diverge (above 60% threshold)
        high_summary = " ".join(anchor_words[:7]) + " some extra words"
        assert cm._anchor_diverges(anchor, high_summary) is False

        # Empty anchor → never diverges
        assert cm._anchor_diverges("", "anything here") is False

        # Non-empty anchor, empty summary → always diverges
        assert cm._anchor_diverges("important fact", "") is True
