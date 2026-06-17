import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _mock_token_count(text: str) -> int:
    """Deterministic stub: 1 token per 4 chars."""
    return max(0, len(text) // 4)


@pytest.mark.asyncio
async def test_build_context_stays_within_budget():
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
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
    with patch("services.memory.context_manager.token_count", side_effect=_mock_token_count):
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
