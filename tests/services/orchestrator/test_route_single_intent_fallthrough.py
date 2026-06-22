"""FIX 5 — route()'s clarification trigger is reserved for genuine MULTI-intent
ambiguity. A skill-less SINGLE intent must fall through to a direct answer
(skills=[], needs_clarification=False) instead of wrongly asking a clarifying
question. These tests pin that refined trigger and guard the multi-intent behavior.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def make_runner(catalog: dict[str, str] | None = None) -> MagicMock:
    catalog = catalog or {"dataset-search": "x", "synthetic-gen": "y"}
    runner = MagicMock()
    runner.catalog = catalog
    runner.catalog_prompt.return_value = "CATALOG"
    runner.tool_schema.return_value = {"type": "function", "function": {"name": "load_skill"}}
    return runner


def make_redis() -> MagicMock:
    redis = MagicMock()
    redis.xadd = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    return redis


@pytest.mark.asyncio
async def test_single_intent_low_confidence_falls_through():
    """One sub-intent, low confidence (1/3) -> answer directly, no clarification."""
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch.object(router, "decompose", AsyncMock(return_value=["explain something"])), \
         patch.object(router, "_confidence_check",
                      AsyncMock(return_value=("dataset-search", 1 / 3))), \
         patch.object(router, "_generate_clarification",
                      AsyncMock(return_value="Which one?")) as gc:
        result = await router.route("explain something")
    assert result.needs_clarification is False
    assert result.skills == []
    assert result.sub_intents == ["explain something"]
    gc.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_intent_no_skill_falls_through():
    """One sub-intent, no skill matched (None, 0.0) -> answer directly, no clarification."""
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch.object(router, "decompose", AsyncMock(return_value=["What is 2+2?"])), \
         patch.object(router, "_confidence_check",
                      AsyncMock(return_value=(None, 0.0))), \
         patch.object(router, "_generate_clarification",
                      AsyncMock(return_value="Which one?")) as gc:
        result = await router.route("What is 2+2? Reply in one sentence.")
    assert result.needs_clarification is False
    assert result.skills == []
    gc.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_intent_confident_skill_routes():
    """Regression: a single confident intent still routes to its skill."""
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch.object(router, "decompose", AsyncMock(return_value=["find a dataset"])), \
         patch.object(router, "_confidence_check",
                      AsyncMock(return_value=("dataset-search", 1.0))):
        result = await router.route("Search the Hugging Face Hub for emotion datasets")
    assert result.needs_clarification is False
    assert result.skills == ["dataset-search"]
    assert result.sub_intents == ["find a dataset"]


@pytest.mark.asyncio
async def test_multi_intent_one_unresolved_still_clarifies():
    """Regression guard: >=2 sub-intents with one unresolved -> clarify, and only the
    flagged sub-intent(s) are handed to the clarifier."""
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch.object(router, "decompose",
                      AsyncMock(return_value=["search dataset", "do undefined thing"])), \
         patch.object(router, "_confidence_check",
                      AsyncMock(side_effect=[
                          ("dataset-search", 1.0),
                          (None, 0.0),
                      ])), \
         patch.object(router, "_generate_clarification",
                      AsyncMock(return_value="What does 'undefined thing' mean?")) as gc:
        result = await router.route("search dataset and do undefined thing")
    assert result.needs_clarification is True
    assert result.skills == []
    gc.assert_awaited_once()
    assert gc.call_args.args[1] == ["do undefined thing"]
