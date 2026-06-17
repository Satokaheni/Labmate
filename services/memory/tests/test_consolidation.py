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
        # First call returns one entry, subsequent calls return None to simulate idle loop
        async def xreadgroup_side_effect(*args, **kwargs):
            if xreadgroup_side_effect.call_count == 0:
                xreadgroup_side_effect.call_count += 1
                return [("consolidate", [("1-0", {"session_id": "s1"})])]
            else:
                # Simulate blocking read that returns nothing — loop continues
                await asyncio.sleep(1)
                return None

        xreadgroup_side_effect.call_count = 0
        redis.xreadgroup = AsyncMock(side_effect=xreadgroup_side_effect)
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
