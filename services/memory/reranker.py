from __future__ import annotations

import asyncio
from functools import lru_cache

RERANK_MODEL = "BAAI/bge-reranker-v2-m3"

# Lazy singleton — avoids GPU model download at import time.
# Tests patch _RERANKER before any call is made.
_RERANKER = None


@lru_cache(maxsize=1)
def _load_reranker():
    from FlagEmbedding import FlagReranker
    return FlagReranker(RERANK_MODEL, use_fp16=True)


async def rerank(query: str, docs: list[str]) -> list[float]:
    """Score (query, doc) pairs with the bge-reranker-v2-m3 cross-encoder.

    Returns one float score per doc, in the same order as `docs`.
    Higher score = more relevant. Runs in asyncio.to_thread (GPU-bound).
    """
    if not docs:
        return []
    model = _RERANKER if _RERANKER is not None else _load_reranker()
    pairs = [[query, doc] for doc in docs]
    scores = await asyncio.to_thread(model.compute_score, pairs)
    return list(scores)
