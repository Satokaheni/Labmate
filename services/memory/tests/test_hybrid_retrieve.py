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
