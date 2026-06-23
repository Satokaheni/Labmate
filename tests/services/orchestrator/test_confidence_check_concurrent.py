"""FIX 10 (A1) — _confidence_check() runs its SELECT_ATTEMPTS samples concurrently
(asyncio.gather) instead of sequentially. The voting logic must be unchanged:
filter Nones, Counter.most_common, confidence = votes / SELECT_ATTEMPTS, and
(None, 0.0) when no sample picked a skill.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.orchestrator.skill_router import SkillRouter, SELECT_ATTEMPTS


def make_runner() -> MagicMock:
    runner = MagicMock()
    runner.catalog = {"dataset-search": "x", "synthetic-gen": "y"}
    runner.catalog_prompt.return_value = "CATALOG"
    runner.tool_schema.return_value = {"type": "function", "function": {"name": "load_skill"}}
    return runner


def make_redis() -> MagicMock:
    redis = MagicMock()
    redis.xadd = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    return redis


def _router() -> SkillRouter:
    return SkillRouter(make_runner(), make_redis(), "http://test/v1")


@pytest.mark.asyncio
async def test_unanimous_winner_full_confidence():
    router = _router()
    router._sample_select = AsyncMock(return_value="dataset-search")
    winner, confidence = await router._confidence_check("find a dataset")
    assert winner == "dataset-search"
    assert confidence == 1.0
    # All SELECT_ATTEMPTS samples were invoked.
    assert router._sample_select.await_count == SELECT_ATTEMPTS


@pytest.mark.asyncio
async def test_majority_winner_partial_confidence():
    router = _router()
    # 2 votes for dataset-search, 1 for synthetic-gen (SELECT_ATTEMPTS == 3).
    router._sample_select = AsyncMock(
        side_effect=["dataset-search", "synthetic-gen", "dataset-search"]
    )
    winner, confidence = await router._confidence_check("find a dataset")
    assert winner == "dataset-search"
    assert confidence == pytest.approx(2 / SELECT_ATTEMPTS)


@pytest.mark.asyncio
async def test_none_votes_count_against_confidence():
    router = _router()
    # One real hit, two misses -> confidence = 1 / SELECT_ATTEMPTS.
    router._sample_select = AsyncMock(side_effect=["dataset-search", None, None])
    winner, confidence = await router._confidence_check("maybe a dataset")
    assert winner == "dataset-search"
    assert confidence == pytest.approx(1 / SELECT_ATTEMPTS)


@pytest.mark.asyncio
async def test_all_none_returns_zero():
    router = _router()
    router._sample_select = AsyncMock(return_value=None)
    winner, confidence = await router._confidence_check("what is 2+2?")
    assert winner is None
    assert confidence == 0.0
    assert router._sample_select.await_count == SELECT_ATTEMPTS


@pytest.mark.asyncio
async def test_samples_run_concurrently():
    """The samples must be in flight simultaneously (gather), not serialized.
    Track max concurrency: with sequential awaits the gauge never exceeds 1."""
    router = _router()
    inflight = 0
    max_inflight = 0

    async def slow_sample(_task, _budget):
        nonlocal inflight, max_inflight
        inflight += 1
        max_inflight = max(max_inflight, inflight)
        await asyncio.sleep(0.02)
        inflight -= 1
        return "dataset-search"

    router._sample_select = slow_sample
    winner, confidence = await router._confidence_check("find a dataset")
    assert winner == "dataset-search"
    assert confidence == 1.0
    assert max_inflight == SELECT_ATTEMPTS
