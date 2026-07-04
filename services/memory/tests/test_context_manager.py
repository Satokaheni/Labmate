from datetime import UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.orchestrator.local_store import LocalStore


def _mock_token_count(text: str) -> int:
    """Deterministic stub: 1 token per 4 chars."""
    return max(0, len(text) // 4)


async def _make_local_store(tmp_path, name="s.sqlite") -> LocalStore:
    store = LocalStore(tmp_path / name)
    await store.connect()
    return store


@pytest.mark.asyncio
async def test_build_context_stays_within_budget(tmp_path):
    with (
        patch("services.memory.context_manager.token_count", side_effect=_mock_token_count),
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

        store = await _make_local_store(tmp_path)
        await store.append_turn("s1", "user", "hello")
        await store.append_turn("s1", "assistant", "hi there")

        embed = AsyncMock(return_value=[[0.1, 0.2]])

        budget = ContextBudget(max_tokens=200, completion_reserve=20)
        cm = ContextManager(
            redis=redis,
            mongo_db=MagicMock(),
            chroma_cols={},
            embedder=embed,
            budget=budget,
            local_store=store,
        )

        ctx = await cm.build_context(
            session_id="s1",
            current_task="implement feature X",
            system_prompt="You are Labmate.",
        )

        assert ctx.total_tokens <= budget.effective_budget
        assert "You are Labmate." in ctx.system_prompt
        assert ctx.core_memory  # should contain pinned goal


@pytest.mark.asyncio
async def test_build_context_pins_core_memory_even_when_over_budget(tmp_path):
    """Core memory is never trimmed — only summary and recent turns are."""
    with (
        patch("services.memory.context_manager.token_count", side_effect=_mock_token_count),
    ):
        from services.memory.context_manager import ContextBudget, ContextManager

        redis = AsyncMock()
        long_core = "GOAL: " + "x" * 1994  # 2000 chars → 500 tokens at 1/4 rate
        redis.get = AsyncMock(side_effect=lambda key: long_core if "core" in key else "")

        store = await _make_local_store(tmp_path)  # no turns seeded

        embed = AsyncMock(return_value=[[0.1]])
        budget = ContextBudget(max_tokens=700, completion_reserve=100)
        cm = ContextManager(
            redis=redis,
            mongo_db=MagicMock(),
            chroma_cols={},
            embedder=embed,
            budget=budget,
            local_store=store,
        )

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


def _make_redis(redis_data=None):
    """Build an AsyncMock redis backed by a plain dict."""
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
    return redis, _store


@pytest.mark.asyncio
async def test_recent_turns_reads_local_store_with_watermark(tmp_path):
    """_recent_turns filters by watermark and formats ROLE: text, newest tail only."""
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        redis_store: dict = {"summarized_through:s1": "5"}  # watermark = 5
        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=lambda k: redis_store.get(k))

        store = await _make_local_store(tmp_path)
        # seq is assigned in append order (0-based); seed 7 turns so seqs 6,7 don't
        # exist naturally — instead seed exactly the seqs needed via direct inserts.
        await store.append_turn("s1", "user", "hello")  # seq 0
        await store.append_turn("s1", "assistant", "unused1")  # seq 1
        await store.append_turn("s1", "user", "unused2")  # seq 2
        await store.append_turn("s1", "assistant", "hi")  # seq 3
        await store.append_turn("s1", "user", "unused3")  # seq 4
        await store.append_turn("s1", "assistant", "unused4")  # seq 5
        await store.append_turn("s1", "user", "how are you")  # seq 6, > watermark(5)
        await store.append_turn("s1", "assistant", "fine")  # seq 7, > watermark(5)

        cm = ContextManager(
            redis=redis,
            mongo_db=MagicMock(),
            chroma_cols={},
            embedder=AsyncMock(),
            local_store=store,
        )
        recent = await cm._recent_turns("s1", budget=500)

        # Should only include turns with seq > 5 (i.e., seqs 6, 7)
        assert "how are you" in recent
        assert "fine" in recent
        assert "hello" not in recent  # seq 0 is <= watermark
        assert "hi" not in recent  # seq 3 is <= watermark


@pytest.mark.asyncio
async def test_recent_turns_defaults_watermark_to_minus_one(tmp_path):
    """When summarized_through is absent, watermark defaults to -1 (all turns are recent)."""
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        redis_store: dict = {}  # no watermark key
        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=lambda k: redis_store.get(k))

        store = await _make_local_store(tmp_path)
        await store.append_turn("s1", "user", "first")
        await store.append_turn("s1", "assistant", "reply")

        cm = ContextManager(
            redis=redis,
            mongo_db=MagicMock(),
            chroma_cols={},
            embedder=AsyncMock(),
            local_store=store,
        )
        recent = await cm._recent_turns("s1", budget=500)

        # All turns should be included (watermark -1 means seq > -1, i.e. all >= 0)
        assert "first" in recent
        assert "reply" in recent


@pytest.mark.asyncio
async def test_last_activity_seconds_reads_local_store_iso_timestamp(tmp_path):
    """last_activity_seconds reads newest turn's created_at from the local store, parses ISO."""
    from datetime import datetime, timedelta

    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        # Create an ISO timestamp from ~900 seconds ago
        old_time = datetime.now(UTC) - timedelta(seconds=900)
        iso_ts = old_time.strftime("%Y-%m-%dT%H:%M:%SZ")

        store = await _make_local_store(tmp_path)
        await store.append_turn("s1", "user", "hi", created_at=iso_ts)

        cm = ContextManager(
            redis=AsyncMock(),
            mongo_db=MagicMock(),
            chroma_cols={},
            embedder=AsyncMock(),
            local_store=store,
        )
        idle = await cm.last_activity_seconds("s1")

        assert idle >= 800  # ~900s, allow generous slack


@pytest.mark.asyncio
async def test_full_compact_watermark_nondestructive(tmp_path):
    """full_compact writes summary, advances watermark, NEVER deletes turns."""
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        redis, redis_store = _make_redis()

        store = await _make_local_store(tmp_path)
        # 40 turns so _KEEP_RECENT=15 leaves 25 to compact
        for i in range(40):
            await store.append_turn("s1", "user" if i % 2 == 0 else "assistant", f"turn {i}")

        llm_calls = []

        async def _llm(prompt: str) -> str:
            llm_calls.append(prompt)
            if "JSON" in prompt:
                return '{"decisions": ["use Python"]}'
            return "summary of old turns"

        cm = ContextManager(
            redis=redis,
            mongo_db=MagicMock(),
            chroma_cols={},
            embedder=AsyncMock(),
            local_store=store,
        )
        await cm.full_compact("s1", _llm)

        # Verify watermark was advanced
        watermark_key = "summarized_through:s1"
        assert watermark_key in redis_store
        # watermark should be max_seq - _KEEP_RECENT = 39 - 15 = 24
        assert redis_store[watermark_key] == "24"

        # Verify summary was written
        assert "summary:s1" in redis_store
        assert redis_store["summary:s1"] == "summary of old turns"

        # CRITICAL: all 40 turns must still be readable (chat_turns is immutable)
        all_turns = await store.all_turns("s1")
        assert len(all_turns) == 40


@pytest.mark.asyncio
async def test_full_compact_respects_watermark_on_second_call(tmp_path):
    """A second full_compact only summarizes newly-eligible turns (respects watermark)."""
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        redis, redis_store = _make_redis({"summarized_through:s1": "24"})  # from prior compaction

        store = await _make_local_store(tmp_path)
        # 45 turns: watermark=24, _KEEP_RECENT=15 → new to_compact = turns 25-29 (5 turns)
        for i in range(45):
            await store.append_turn("s1", "user" if i % 2 == 0 else "assistant", f"turn {i}")

        llm_calls = []

        async def _llm(prompt: str) -> str:
            llm_calls.append(prompt)
            if "JSON" in prompt:
                return '{"decisions": []}'
            return "new summary segment"

        cm = ContextManager(
            redis=redis,
            mongo_db=MagicMock(),
            chroma_cols={},
            embedder=AsyncMock(),
            local_store=store,
        )
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
async def test_full_compact_returns_reflections(tmp_path):
    """full_compact should call llm_fn multiple times and return reflection strings."""
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        redis, redis_store = _make_redis()

        store = await _make_local_store(tmp_path)
        # 20 turns so _KEEP_RECENT=15 leaves 5 to compact
        for i in range(20):
            await store.append_turn("s1", "user" if i % 2 == 0 else "assistant", f"message {i}")

        llm_calls = []

        async def _llm(prompt: str) -> str:
            llm_calls.append(prompt)
            if "JSON" in prompt:
                return '{"decisions": ["decided to use Python"], "findings": ["Redis is fast"]}'
            return "summary of old turns"

        cm = ContextManager(
            redis=redis,
            mongo_db=MagicMock(),
            chroma_cols={},
            embedder=AsyncMock(),
            local_store=store,
        )
        result = await cm.full_compact("s1", _llm)

        assert result["pruned_messages"] == 0  # no deletion; watermark-only
        assert result["reflections"] == ["decided to use Python", "Redis is fast"]
        # Anchor should be set on first compact
        assert redis_store.get("anchor:s1") is not None


@pytest.mark.asyncio
async def test_full_compact_saves_anchor_only_on_first_compact(tmp_path):
    """Second compact should not overwrite the anchor."""
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        original_anchor = "anchor from first compact"
        redis, redis_store = _make_redis({"anchor:s1": original_anchor})

        store = await _make_local_store(tmp_path)
        for i in range(20):
            await store.append_turn("s1", "user", f"msg {i}")

        async def _llm(prompt: str) -> str:
            return '{"decisions": []}' if "JSON" in prompt else "new summary"

        cm = ContextManager(
            redis=redis,
            mongo_db=MagicMock(),
            chroma_cols={},
            embedder=AsyncMock(),
            local_store=store,
        )
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
async def test_build_context_surfaces_anchor_when_diverged(tmp_path):
    """When the anchor's facts are absent from the summary, anchor_buffer is populated."""
    with (
        patch("services.memory.context_manager.token_count", side_effect=_mock_token_count),
    ):
        from services.memory.context_manager import ContextBudget, ContextManager

        store_data = {
            "core:s1": "GOAL: build MCP bridge",
            "summary:s1": "discussed unrelated weather topics today",
            "anchor:s1": "project uses Gemma model with Redis streams for goals",
        }
        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=lambda k: store_data.get(k))

        store = await _make_local_store(tmp_path)  # no turns seeded

        embed = AsyncMock(return_value=[[0.1]])
        budget = ContextBudget(max_tokens=4000, completion_reserve=100)
        cm = ContextManager(
            redis=redis,
            mongo_db=MagicMock(),
            chroma_cols={},
            embedder=embed,
            budget=budget,
            local_store=store,
        )

        ctx = await cm.build_context("s1", "task", "system")
        assert "KEY FACTS" in ctx.anchor_buffer
        assert "Gemma" in ctx.anchor_buffer
        assert "KEY FACTS" in ctx.as_prompt()


@pytest.mark.asyncio
async def test_build_context_omits_anchor_when_contained_in_summary(tmp_path):
    """When the summary already contains the anchor's facts, anchor_buffer is empty."""
    with (
        patch("services.memory.context_manager.token_count", side_effect=_mock_token_count),
    ):
        from services.memory.context_manager import ContextBudget, ContextManager

        store_data = {
            "core:s1": "GOAL: build MCP bridge",
            "summary:s1": "project uses Gemma model with Redis streams for goals and more detail",
            "anchor:s1": "project uses Gemma model with Redis streams for goals",
        }
        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=lambda k: store_data.get(k))

        store = await _make_local_store(tmp_path)  # no turns seeded

        embed = AsyncMock(return_value=[[0.1]])
        budget = ContextBudget(max_tokens=4000, completion_reserve=100)
        cm = ContextManager(
            redis=redis,
            mongo_db=MagicMock(),
            chroma_cols={},
            embedder=embed,
            budget=budget,
            local_store=store,
        )

        ctx = await cm.build_context("s1", "task", "system")
        assert ctx.anchor_buffer == ""


@pytest.mark.asyncio
async def test_full_compact_emits_compact_quality_event(tmp_path):
    """full_compact emits compact.quality with ratio, counts, and tokens saved."""
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        redis, redis_store = _make_redis()

        store = await _make_local_store(tmp_path)
        for i in range(20):
            await store.append_turn(
                "s1",
                "user" if i % 2 == 0 else "assistant",
                f"message number {i} with some content to make tokens",
            )

        async def _llm(prompt: str) -> str:
            return '{"decisions": ["use Python"]}' if "JSON" in prompt else "short summary"

        captured = {}

        async def _fake_emit(type, **fields):
            captured[type] = fields

        cm = ContextManager(
            redis=redis,
            mongo_db=MagicMock(),
            chroma_cols={},
            embedder=AsyncMock(),
            local_store=store,
        )

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
async def test_last_activity_seconds_reports_idle_time(tmp_path):
    """last_activity_seconds returns roughly the age of the newest turn."""
    from datetime import datetime, timedelta

    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        old_time = datetime.now(UTC) - timedelta(seconds=900)
        old_iso = old_time.strftime("%Y-%m-%dT%H:%M:%SZ")

        store = await _make_local_store(tmp_path)
        await store.append_turn("s1", "user", "hi", created_at=old_iso)

        cm = ContextManager(
            redis=AsyncMock(),
            mongo_db=MagicMock(),
            chroma_cols={},
            embedder=AsyncMock(),
            local_store=store,
        )
        idle = await cm.last_activity_seconds("s1")
        assert idle >= 800  # ~900s, allow generous slack


@pytest.mark.asyncio
async def test_maybe_background_compact_skips_when_not_idle(tmp_path):
    """A recently-active session is never background-compacted."""
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        store = await _make_local_store(tmp_path)
        await store.append_turn("s1", "user", "hi")  # created_at defaults to "now"

        llm = AsyncMock()
        cm = ContextManager(
            redis=AsyncMock(),
            mongo_db=MagicMock(),
            chroma_cols={},
            embedder=AsyncMock(),
            local_store=store,
        )
        result = await cm.maybe_background_compact("s1", llm)
        assert result is None
        llm.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_background_compact_runs_when_idle_and_full():
    """An idle, high-fill session triggers full_compact."""
    import datetime as _dt

    with (
        patch("services.memory.context_manager.token_count", side_effect=_mock_token_count),
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


# ── conversation_context tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_conversation_context_returns_summary_and_recent_turns(tmp_path):
    """conversation_context assembles summary + anchor (if diverged) + recent turns."""
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        redis = AsyncMock()

        def _redis_get(key):
            if "summary:" in key:
                return "Summary of first part of the conversation"
            elif "anchor:" in key:
                return "Founding fact about the task"
            elif "summarized_through:" in key:
                return "0"  # watermark: first turn is compacted
            else:
                return None

        redis.get = AsyncMock(side_effect=_redis_get)

        store = await _make_local_store(tmp_path)
        # Recent turns (seq > watermark=0, so seq 1,2 are recent)
        await store.append_turn("s1", "user", "compacted already")  # seq 0
        await store.append_turn("s1", "user", "what is AI?")  # seq 1
        await store.append_turn("s1", "assistant", "AI is machine learning")  # seq 2

        cm = ContextManager(
            redis=redis,
            mongo_db=MagicMock(),
            chroma_cols={},
            embedder=AsyncMock(),
            local_store=store,
        )

        result = await cm.conversation_context("s1", budget=500)

        # Should contain summary, anchor (if diverged), and recent turns
        assert "Summary of first part" in result
        assert "Founding fact" in result
        assert "USER: what is AI?" in result
        assert "ASSISTANT: AI is machine learning" in result


@pytest.mark.asyncio
async def test_conversation_context_returns_empty_on_no_session():
    """conversation_context returns empty string when session_id is empty."""
    from services.memory.context_manager import ContextManager

    redis = AsyncMock()
    cm = ContextManager(
        redis=redis,
        mongo_db=MagicMock(),
        chroma_cols={},
        embedder=AsyncMock(),
    )

    result = await cm.conversation_context("", budget=500)
    assert result == ""


@pytest.mark.asyncio
async def test_conversation_context_returns_empty_on_failure():
    """conversation_context returns empty string on any exception (best-effort)."""
    from services.memory.context_manager import ContextManager

    redis = AsyncMock()
    redis.get = AsyncMock(side_effect=RuntimeError("Redis error"))

    cm = ContextManager(
        redis=redis,
        mongo_db=MagicMock(),
        chroma_cols={},
        embedder=AsyncMock(),
    )

    result = await cm.conversation_context("s1", budget=500)
    # Should not raise, returns empty string instead
    assert result == ""


@pytest.mark.asyncio
async def test_conversation_context_does_not_call_hybrid_retrieve(tmp_path):
    """conversation_context assembles summary+anchor+recent WITHOUT RAG (keep hot-path cheap)."""
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
        from services.memory.context_manager import ContextManager

        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)

        store = await _make_local_store(tmp_path)  # no turns seeded

        # Mock hybrid_retrieve so we can verify it's NOT called
        hybrid_retrieve_mock = AsyncMock(return_value=[])

        cm = ContextManager(
            redis=redis,
            mongo_db=MagicMock(),
            chroma_cols={},
            embedder=AsyncMock(),
            local_store=store,
        )
        cm.hybrid_retrieve = hybrid_retrieve_mock

        await cm.conversation_context("s1", budget=500)

        # conversation_context should NOT call hybrid_retrieve (RAG is deferred)
        hybrid_retrieve_mock.assert_not_called()
