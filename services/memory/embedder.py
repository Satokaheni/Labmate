from __future__ import annotations

import asyncio
import hashlib
import json
from functools import lru_cache

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384
_CACHE_TTL = 3600

# Lazy singleton — not loaded at import time; tests can patch _MODEL freely.
_MODEL = None


@lru_cache(maxsize=1)
def _load_model():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(EMBED_MODEL, device="cuda")


async def embed(
    texts: list[str],
    redis=None,
) -> list[list[float]]:
    """Embed a batch of texts using BAAI/bge-small-en-v1.5.

    Results are cached in Redis by SHA-256(text) with a 3600s TTL to avoid
    re-embedding identical content. Pass redis=None to skip caching.
    Encoding is CPU-bound; runs in asyncio.to_thread to avoid blocking the
    event loop.
    """
    results: list[list[float] | None] = [None] * len(texts)
    uncached_indices: list[int] = []
    uncached_texts: list[str] = []

    if redis is not None:
        for i, text in enumerate(texts):
            key = f"embed_cache:{hashlib.sha256(text.encode()).hexdigest()}"
            cached = await redis.get(key)
            if cached is not None:
                results[i] = json.loads(cached)
            else:
                uncached_indices.append(i)
                uncached_texts.append(text)
    else:
        uncached_indices = list(range(len(texts)))
        uncached_texts = texts

    if uncached_texts:
        model = _MODEL if _MODEL is not None else _load_model()
        result = await asyncio.to_thread(
            lambda: model.encode(
                uncached_texts,
                normalize_embeddings=True,
                batch_size=64,
            )
        )
        # Handle both numpy arrays and lists (latter for mocked tests)
        vectors = result.tolist() if hasattr(result, 'tolist') else result
        for i, (idx, text) in enumerate(zip(uncached_indices, uncached_texts)):
            results[idx] = vectors[i]
            if redis is not None:
                key = f"embed_cache:{hashlib.sha256(text.encode()).hexdigest()}"
                await redis.setex(key, _CACHE_TTL, json.dumps(vectors[i]))

    return results  # type: ignore[return-value]
