import asyncio
import pytest
from unittest.mock import MagicMock, patch


@pytest.mark.asyncio
async def test_rerank_returns_scores_per_pair():
    mock_reranker = MagicMock()
    mock_reranker.compute_score.return_value = [0.9, 0.2, 0.7]

    with patch("services.memory.reranker._RERANKER", mock_reranker):
        from services.memory.reranker import rerank
        scores = await rerank("my query", ["doc a", "doc b", "doc c"])

    assert len(scores) == 3
    assert scores[0] == pytest.approx(0.9)
    assert scores[1] == pytest.approx(0.2)
    assert scores[2] == pytest.approx(0.7)


@pytest.mark.asyncio
async def test_rerank_passes_query_doc_pairs():
    mock_reranker = MagicMock()
    mock_reranker.compute_score.return_value = [0.5]

    with patch("services.memory.reranker._RERANKER", mock_reranker):
        from services.memory.reranker import rerank
        await rerank("query", ["doc"])

    call_args = mock_reranker.compute_score.call_args[0][0]
    assert call_args == [["query", "doc"]]


@pytest.mark.asyncio
async def test_rerank_empty_docs_returns_empty():
    mock_reranker = MagicMock()
    mock_reranker.compute_score.return_value = []

    with patch("services.memory.reranker._RERANKER", mock_reranker):
        from services.memory.reranker import rerank
        scores = await rerank("query", [])

    assert scores == []
    mock_reranker.compute_score.assert_not_called()
