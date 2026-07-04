from __future__ import annotations

import asyncio
from functools import lru_cache

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384

# Lazy singleton — not loaded at import time; tests can patch _MODEL freely.
_MODEL = None


@lru_cache(maxsize=1)
def _load_model():
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(EMBED_MODEL, device="cuda")


async def embed(
    texts: list[str],
) -> list[list[float]]:
    """Embed a batch of texts using BAAI/bge-small-en-v1.5.

    Encoding is CPU-bound; runs in asyncio.to_thread to avoid blocking the
    event loop.
    """
    model = _MODEL if _MODEL is not None else _load_model()
    result = await asyncio.to_thread(
        lambda: model.encode(
            texts,
            normalize_embeddings=True,
            batch_size=64,
        )
    )
    # Handle both numpy arrays and lists (latter for mocked tests)
    vectors = result.tolist() if hasattr(result, "tolist") else result
    return vectors
