"""Unit tests for SkillPreGate — semantic pre-gate for skill routing."""

import pytest

from services.orchestrator.routing_pregate import SkillPreGate


def _fake_embed(vectors):
    """Factory for deterministic test embeddings (GPU-free)."""

    async def _e(texts, redis=None):
        return [vectors[t] for t in texts]

    return _e


@pytest.mark.asyncio
async def test_close_task_is_plausible():
    """A task semantically close to a catalog description returns True."""
    cat = {"code-review": "review code for bugs and quality issues"}
    emb = _fake_embed(
        {
            "code-review: review code for bugs and quality issues": [1.0, 0.0],
            "please review my code for bugs": [0.98, 0.198],  # cos ~0.98
        }
    )
    gate = SkillPreGate(cat, threshold=0.5, embed_fn=emb)
    assert await gate.any_plausible_skill("please review my code for bugs") is True


@pytest.mark.asyncio
async def test_offtopic_task_is_implausible():
    """An off-topic task ("what is the capital of France") returns False."""
    cat = {"code-review": "review code for bugs and quality issues"}
    emb = _fake_embed(
        {
            "code-review: review code for bugs and quality issues": [1.0, 0.0],
            "what is the capital of France": [0.0, 1.0],  # cos 0.0
        }
    )
    gate = SkillPreGate(cat, threshold=0.5, embed_fn=emb)
    assert await gate.any_plausible_skill("what is the capital of France") is False


@pytest.mark.asyncio
async def test_empty_catalog_is_implausible():
    """An empty catalog returns False."""
    gate = SkillPreGate({}, threshold=0.5, embed_fn=_fake_embed({}))
    assert await gate.any_plausible_skill("anything") is False


@pytest.mark.asyncio
async def test_embed_failure_fails_safe_to_true():
    """An embed_fn that raises makes any_plausible_skill return True (fail-safe)."""

    async def _boom(texts, redis=None):
        raise RuntimeError("embed down")

    gate = SkillPreGate({"x": "y"}, threshold=0.5, embed_fn=_boom)
    assert await gate.any_plausible_skill("anything") is True  # never skip on failure


# ---------------------------------------------------------------------------
# max_similarity tests (new method)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_similarity_close_task_returns_high_value():
    """A task close to a skill returns high cosine similarity."""
    cat = {"code-review": "review code for bugs and quality issues"}
    emb = _fake_embed(
        {
            "code-review: review code for bugs and quality issues": [1.0, 0.0],
            "please review my code for bugs": [0.98, 0.198],  # cos ~0.98
        }
    )
    gate = SkillPreGate(cat, threshold=0.5, embed_fn=emb)
    sim = await gate.max_similarity("please review my code for bugs")
    assert sim >= 0.9


@pytest.mark.asyncio
async def test_max_similarity_offtopic_task_returns_low_value():
    """An off-topic task returns near-zero cosine similarity."""
    cat = {"code-review": "review code for bugs and quality issues"}
    emb = _fake_embed(
        {
            "code-review: review code for bugs and quality issues": [1.0, 0.0],
            "what is the capital of France": [0.0, 1.0],  # cos 0.0
        }
    )
    gate = SkillPreGate(cat, threshold=0.5, embed_fn=emb)
    sim = await gate.max_similarity("what is the capital of France")
    assert sim < 0.1


@pytest.mark.asyncio
async def test_max_similarity_empty_catalog_returns_neg_inf():
    """Empty catalog → float('-inf') so any_plausible_skill is False for any threshold."""
    gate = SkillPreGate({}, threshold=0.5, embed_fn=_fake_embed({}))
    sim = await gate.max_similarity("anything")
    assert sim == float("-inf")


@pytest.mark.asyncio
async def test_max_similarity_embed_error_returns_pos_inf():
    """Embed error → float('inf') so any_plausible_skill is True (fail-safe)."""

    async def _boom(texts, redis=None):
        raise RuntimeError("embed down")

    gate = SkillPreGate({"x": "y"}, threshold=0.5, embed_fn=_boom)
    sim = await gate.max_similarity("anything")
    assert sim == float("inf")
