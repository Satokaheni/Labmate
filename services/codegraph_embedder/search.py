"""Hybrid BM25 + dense search over code_symbols Chroma collection."""
from __future__ import annotations

from rank_bm25 import BM25Okapi

from services.memory.embedder import embed
from services.memory.reranker import rerank


async def hybrid_code_search(
    query: str,
    chroma_col,
    k: int = 8,
) -> list[dict]:
    """Dense top-50 → BM25 on candidates → RRF fusion → cross-encoder rerank."""
    vec = (await embed([query]))[0]
    n_candidates = min(50, await chroma_col.count())
    if n_candidates == 0:
        return []
    results = await chroma_col.query(
        query_embeddings=[vec],
        n_results=n_candidates,
        include=["documents", "metadatas", "distances"],
    )
    docs      = results["documents"][0]
    metadatas = results["metadatas"][0]
    ids       = results["ids"][0]

    if not docs:
        return []

    tokenised   = [d.lower().split() for d in docs]
    bm25        = BM25Okapi(tokenised)
    bm25_scores = bm25.get_scores(query.lower().split())

    dense_rank = {id_: i for i, id_ in enumerate(ids)}
    bm25_rank  = {
        ids[i]: rank
        for rank, i in enumerate(
            sorted(range(len(ids)), key=lambda x: bm25_scores[x], reverse=True)
        )
    }

    rrf: dict[str, float] = {
        id_: (
            1.0 / (60 + dense_rank.get(id_, 999)) +
            1.0 / (60 + bm25_rank.get(id_, 999))
        )
        for id_ in ids
    }

    shortlist_ids  = sorted(rrf, key=rrf.__getitem__, reverse=True)[:20]
    idx_map        = {id_: i for i, id_ in enumerate(ids)}
    shortlist_docs  = [docs[idx_map[i]]      for i in shortlist_ids]
    shortlist_meta  = [metadatas[idx_map[i]] for i in shortlist_ids]

    scores = await rerank(query, shortlist_docs)
    ranked = sorted(zip(scores, shortlist_docs, shortlist_meta), key=lambda t: t[0], reverse=True)

    return [
        {
            "score":          float(score),
            "text":           doc,
            "file_path":      meta["file_path"],
            "kind":           meta["kind"],
            "name":           meta["name"],
            "qualified_name": meta["qualified_name"],
            "start_line":     meta["start_line"],
            "end_line":       meta["end_line"],
        }
        for score, doc, meta in ranked[:k]
    ]
