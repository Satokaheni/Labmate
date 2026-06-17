import asyncio
import json
import hashlib
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_redis_mock():
    r = AsyncMock()
    r.get = AsyncMock(return_value=None)
    r.setex = AsyncMock()
    return r


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


@pytest.mark.asyncio
async def test_embed_uses_redis_cache_on_hit():
    mock_model = MagicMock()
    mock_model.encode.return_value = [[0.9, 0.8, 0.7]]
    redis = _make_redis_mock()
    cached_vec = [0.1, 0.2, 0.3]
    redis.get = AsyncMock(return_value=json.dumps(cached_vec))

    with patch("services.memory.embedder._MODEL", mock_model):
        from services.memory.embedder import embed
        result = await embed(["hello"], redis=redis)

    # Cache hit — model.encode must NOT be called
    mock_model.encode.assert_not_called()
    assert result[0] == cached_vec


@pytest.mark.asyncio
async def test_embed_writes_to_cache_on_miss():
    mock_model = MagicMock()
    mock_model.encode.return_value = [[0.1, 0.2, 0.3]]
    redis = _make_redis_mock()

    with patch("services.memory.embedder._MODEL", mock_model):
        from services.memory.embedder import embed
        await embed(["hello"], redis=redis)

    # Cache miss — setex must be called with the vector
    redis.setex.assert_called_once()
    call_args = redis.setex.call_args[0]
    assert call_args[1] == 3600
    assert json.loads(call_args[2]) == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_embed_cache_key_is_sha256_of_text():
    mock_model = MagicMock()
    mock_model.encode.return_value = [[0.1]]
    redis = _make_redis_mock()

    with patch("services.memory.embedder._MODEL", mock_model):
        from services.memory.embedder import embed
        await embed(["hello"], redis=redis)

    expected_hash = hashlib.sha256("hello".encode()).hexdigest()
    expected_key = f"embed_cache:{expected_hash}"
    # get was called with this key
    redis.get.assert_called_once_with(expected_key)
