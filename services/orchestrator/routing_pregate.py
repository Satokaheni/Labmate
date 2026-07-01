"""Semantic pre-gate: skip skill routing when no skill plausibly matches the task.

The SELECT_ATTEMPTS skill-routing vote is expensive (validated ~19s on a no-match task
that falls through to direct answer anyway). This gate embeds the task once and cosine-
matches it against the pre-embedded catalog; below threshold, route() skips the vote.
FAIL-SAFE: any embed error returns True (proceed to the full vote) — never skip on doubt.
"""

from __future__ import annotations

import os

from services.memory.embedder import embed

PREGATE_SIM_THRESHOLD = float(os.getenv("PREGATE_SIM_THRESHOLD", "0.30"))


class SkillPreGate:
    def __init__(self, catalog, *, redis=None, threshold=PREGATE_SIM_THRESHOLD, embed_fn=embed):
        # catalog: {skill_name: description}. Sorted for deterministic embedding order.
        self._entries = sorted(catalog.items())
        self._redis = redis
        self._threshold = threshold
        self._embed_fn = embed_fn
        self._cat_vecs: list[list[float]] | None = None

    async def _ensure_catalog(self) -> None:
        if self._cat_vecs is not None or not self._entries:
            return
        texts = [f"{name}: {desc}" for name, desc in self._entries]
        self._cat_vecs = await self._embed_fn(texts, self._redis)

    async def max_similarity(self, task: str) -> float:
        """Return the best cosine similarity between *task* and any catalog entry.

        Returns:
            float("-inf")  — empty catalog (any_plausible_skill → False for any threshold)
            float("inf")   — embed error (FAIL-SAFE → any_plausible_skill → True)
            otherwise      — best dot-product score over the L2-normalised catalog vecs
        """
        if not self._entries:
            return float("-inf")
        try:
            await self._ensure_catalog()
            (task_vec,) = await self._embed_fn([task], self._redis)
            return max(_dot(task_vec, v) for v in (self._cat_vecs or []))
        except Exception:  # noqa: BLE001 — fail-safe: proceed to the full vote
            return float("inf")

    async def any_plausible_skill(self, task: str) -> bool:
        return await self.max_similarity(task) >= self._threshold


def _dot(a, b) -> float:
    # embeddings are L2-normalized, so dot product == cosine similarity
    return sum(x * y for x, y in zip(a, b, strict=True))
