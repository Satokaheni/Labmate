import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _mock_token_count(text: str) -> int:
    """Deterministic stub: 1 token per 4 chars."""
    return max(0, len(text) // 4)


class _AsyncIter:
    """Async iterator that also supports Motor's chainable .sort(), .skip(), and .limit()."""
    def __init__(self, docs):
        self._docs = iter(docs)
    def __aiter__(self): return self
    async def __anext__(self):
        try:
            return next(self._docs)
        except StopIteration:
            raise StopAsyncIteration
    def sort(self, *args, **kwargs): return self
    def skip(self, *args, **kwargs): return self
    def limit(self, *args, **kwargs): return self


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

        # The cursor is used by _recent_turns which calls .find().sort().limit()
        # We need the full chain to return our AsyncDocIter
        mock_cursor = AsyncDocIter(turns)
        mock_sort = MagicMock()
        mock_sort.limit = MagicMock(return_value=mock_cursor)
        mock_find = MagicMock()
        mock_find.sort = MagicMock(return_value=mock_sort)
        db.messages.find = MagicMock(return_value=mock_find)

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
        from services.memory.context_manager import ContextManager, ContextBudget

        redis = AsyncMock()
        long_core = "GOAL: " + "x" * 1994  # 2000 chars → 500 tokens at 1/4 rate
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
        cm = ContextManager(redis=redis, mongo_db=db, chroma_cols={}, embedder=embed, budget=budget)

        ctx = await cm.build_context("s1", "task", "system")
        assert ctx.core_memory == long_core


def test_trim_to_budget_drops_oldest_lines():
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        cm = ContextManager(
            redis=AsyncMock(), mongo_db=MagicMock(),
            chroma_cols={}, embedder=AsyncMock(),
        )
        # 5 lines × ~13 chars each → ~3 tokens each (at 4 chars/token).
        # Total ~15 tokens. Budget=6 → keep newest lines.
        text = "\n".join([f"line {i} text" for i in range(5)])
        result = cm._trim_to_budget(text, budget=6)
        lines = [l for l in result.splitlines() if l]
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
    def __aiter__(self): return self
    async def __anext__(self):
        try:
            return next(self._docs)
        except StopIteration:
            raise StopAsyncIteration
    def sort(self, *args, **kwargs): return self
    def skip(self, *args, **kwargs): return self
    def limit(self, *args, **kwargs): return self


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
    db.__getitem__ = MagicMock(return_value=MagicMock(
        find=find_mock,
        update_one=AsyncMock(),
        delete_many=AsyncMock(),
    ))

    return ContextManager(redis=redis, mongo_db=db, chroma_cols={}, embedder=AsyncMock())


@pytest.mark.asyncio
async def test_clear_tool_results_strips_large_tool_messages():
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)

        db = MagicMock()
        big_content = "x" * 1000  # well above _TOOL_RESULT_THRESHOLD (600)
        tool_docs = [
            {"_id": "t1", "content": big_content},
            {"_id": "t2", "content": "short"},   # below threshold, should not be stripped
        ]
        messages_col = MagicMock()
        messages_col.find = MagicMock(return_value=_AsyncIter(tool_docs))
        messages_col.update_one = AsyncMock()
        db.__getitem__ = MagicMock(return_value=messages_col)

        cm = ContextManager(redis=redis, mongo_db=db, chroma_cols={}, embedder=AsyncMock())
        freed = await cm.clear_tool_results("s1")

        assert freed > 0
        # update_one called once (only the large doc)
        assert messages_col.update_one.call_count == 1
        call_args = messages_col.update_one.call_args
        assert call_args[0][0] == {"_id": "t1"}
        assert "stripped" in call_args[0][1]["$set"]


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
            {"_id": str(i), "seq": i, "role": "user" if i % 2 == 0 else "assistant",
             "content": f"message {i}"}
            for i in range(20)
        ]
        messages_col = MagicMock()
        # clear_tool_results also calls find — return empty for role='tool' query
        call_count = [0]
        def _find(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _AsyncIter([])   # tool result query → empty
            return _AsyncIter(turns)    # full compact query
        messages_col.find = MagicMock(side_effect=_find)
        messages_col.update_one = AsyncMock()
        messages_col.delete_many = AsyncMock()
        db.__getitem__ = MagicMock(return_value=messages_col)

        llm_calls = []
        async def _llm(prompt: str) -> str:
            llm_calls.append(prompt)
            if "JSON" in prompt:
                return '{"decisions": ["decided to use Python"], "findings": ["Redis is fast"]}'
            return "summary of old turns"

        cm = ContextManager(redis=redis, mongo_db=db, chroma_cols={}, embedder=AsyncMock())
        result = await cm.full_compact("s1", _llm)

        assert result["pruned_messages"] == 5
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
        turns = [
            {"_id": str(i), "seq": i, "role": "user", "content": f"msg {i}"}
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

        cm = ContextManager(redis=AsyncMock(), mongo_db=MagicMock(), chroma_cols={}, embedder=AsyncMock())
        # 45 turns → 3 blocks of 20/20/5 → 3 block calls + 1 merge call = 4 total
        turns = [{"role": "user", "content": f"msg {i}"} for i in range(45)]
        llm_calls = []

        async def _llm(prompt: str) -> str:
            llm_calls.append(prompt)
            return f"summary_{len(llm_calls)}"

        result = await cm._parallel_summarize(turns, anchor="", llm_fn=_llm)
        assert len(llm_calls) == 4   # 3 blocks + 1 merge
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
        core_pos    = prompt.index("goalcore")
        anchor_pos  = prompt.index("anchorfacts")
        rag_pos     = prompt.index("ragtext")
        summary_pos = prompt.index("summarytext")
        recent_pos  = prompt.index("recenttext")
        assert core_pos < anchor_pos < rag_pos < summary_pos < recent_pos


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
        old_docs = [{"_id": "old1", "content": "x" * 1000}]
        messages_col = MagicMock()
        messages_col.find = MagicMock(return_value=RecordingCursor(old_docs))
        messages_col.update_one = AsyncMock()
        db.__getitem__ = MagicMock(return_value=messages_col)

        cm = ContextManager(redis=redis, mongo_db=db, chroma_cols={}, embedder=AsyncMock())
        freed = await cm.clear_tool_results("s1")

        assert recorded["sort"] == ("seq", -1)
        assert recorded["skip"] == cm._KEEP_RECENT_TOOL_RESULTS
        assert recorded["skip"] == 10
        assert freed > 0
        assert messages_col.update_one.call_count == 1


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
        assert 0.0 <= evt["compression_ratio"] <= 1.0
        # Return contract is unchanged
        assert result["pruned_messages"] == 5
        assert result["reflections"] == ["use Python"]


@pytest.mark.asyncio
async def test_last_activity_seconds_reports_idle_time():
    """last_activity_seconds returns roughly the age of the newest message."""
    import time as _time
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        old_ts = _time.time() - 900.0  # float Unix timestamp, as actually stored
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


def test_anchor_diverges_heuristic_boundaries():
    """_anchor_diverges uses a 60% word-overlap threshold on tokens longer than 3 chars."""
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        cm = ContextManager(redis=AsyncMock(), mongo_db=MagicMock(), chroma_cols={}, embedder=AsyncMock())

        # Build anchor with 10 distinct >3-char words
        anchor_words = ["alpha", "bravo", "charlie", "delta", "echo",
                        "foxtrot", "golf", "hotel", "india", "juliet"]
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
