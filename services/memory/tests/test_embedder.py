from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_embed_returns_vectors():
    mock_model = MagicMock()
    mock_model.encode.return_value = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]

    with patch("services.memory.embedder._MODEL", mock_model):
        from services.memory.embedder import embed

        result = await embed(["hello", "world"])

    assert len(result) == 2
    assert result[0] == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_embed_calls_encode_with_normalize():
    mock_model = MagicMock()
    mock_model.encode.return_value = [[0.1, 0.2]]

    with patch("services.memory.embedder._MODEL", mock_model):
        from services.memory.embedder import embed

        await embed(["test"])

    mock_model.encode.assert_called_once_with(
        ["test"],
        normalize_embeddings=True,
        batch_size=64,
    )
