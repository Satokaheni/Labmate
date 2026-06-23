"""A/B routing — route(mode=...) gating in skill_router.

mode="single" (now the DEFAULT) must skip decompose() and use sub_intents=[task];
mode="multi" (the explicit fallback) must call decompose() exactly as today. NEW
file; touches no protected method.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.orchestrator.skill_router import SkillRouter


def make_runner() -> MagicMock:
    runner = MagicMock()
    runner.catalog = {"dataset-search": "find datasets"}
    runner.catalog_prompt.return_value = "CATALOG"
    runner.tool_schema.return_value = {"type": "function", "function": {"name": "load_skill"}}
    return runner


def make_redis() -> MagicMock:
    redis = MagicMock()
    redis.xadd = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    return redis


@pytest.mark.asyncio
async def test_route_single_skips_decompose_uses_whole_task():
    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    router.decompose = AsyncMock(return_value=["SHOULD-NOT-BE-USED"])
    # Force the post-decompose path to flag (skill-less) -> single intent fall-through.
    router._confidence_check = AsyncMock(return_value=(None, 0.0))

    task = "Write a function that reverses a string, and write a pytest unit test for it"
    result = await router.route(task, mode="single")

    router.decompose.assert_not_awaited()
    assert result.sub_intents == [task]


@pytest.mark.asyncio
async def test_route_default_is_single_skips_decompose():
    # Default flipped to "single": route() with NO mode must SKIP decompose() and use
    # the whole task as one intent. (multi remains reachable via explicit mode="multi".)
    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    router.decompose = AsyncMock(return_value=["SHOULD-NOT-BE-USED"])
    router._confidence_check = AsyncMock(return_value=(None, 0.0))

    result = await router.route("do a and b")  # default mode == "single"

    router.decompose.assert_not_awaited()
    assert result.sub_intents == ["do a and b"]


@pytest.mark.asyncio
async def test_route_explicit_multi_calls_decompose():
    # multi fallback: explicitly selecting mode="multi" still decomposes, byte-for-byte.
    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    router.decompose = AsyncMock(return_value=["a", "b"])
    router._confidence_check = AsyncMock(return_value=("dataset-search", 1.0))

    result = await router.route("do a and b", mode="multi")

    router.decompose.assert_awaited_once_with("do a and b")
    assert result.sub_intents == ["a", "b"]
