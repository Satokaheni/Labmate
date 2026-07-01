"""Tests for SkillPreGate wiring into route() (mocked, no GPU required)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.orchestrator import skill_router as SR
from services.orchestrator.skill_router import SkillRouter
from services.skill_runner.skill_runner import SkillMeta


def _router():
    """Create a minimal SkillRouter with a mock catalog."""
    runner = MagicMock()
    runner.catalog = {
        "code-review": SkillMeta(
            name="code-review",
            description="review code",
            path=Path("/fake/SKILL.md"),
            tier="bundled",
        )
    }
    return SkillRouter(runner=runner, redis=AsyncMock(), gemma_api_base="http://x/v1")


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_pregate_skips_vote_when_implausible(monkeypatch):
    """When pre-gate returns False, route() skips _confidence_check and returns empty result."""
    monkeypatch.setattr(SR, "ENABLE_ROUTING_PREGATE", True)
    router = _router()
    router._pregate = AsyncMock()
    router._pregate.any_plausible_skill.return_value = False

    with patch.object(router, "_confidence_check", side_effect=AssertionError("vote must not run")):
        res = await router.route("what is the capital of France")

    assert res.skills == []
    assert not res.needs_clarification
    assert res.sub_intents == ["what is the capital of France"]


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_pregate_allows_vote_when_plausible(monkeypatch):
    """When pre-gate returns True, route() runs the normal _confidence_check vote."""
    monkeypatch.setattr(SR, "ENABLE_ROUTING_PREGATE", True)
    router = _router()
    router._pregate = AsyncMock()
    router._pregate.any_plausible_skill.return_value = True

    with patch.object(
        router, "_confidence_check", new=AsyncMock(return_value=("code-review", 1.0))
    ):
        res = await router.route("review my code")

    assert res.skills == ["code-review"]
    assert res.sub_intents == ["review my code"]


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_flag_off_never_consults_pregate(monkeypatch):
    """When flag is OFF, pre-gate is never consulted (byte-identical to today)."""
    monkeypatch.setattr(SR, "ENABLE_ROUTING_PREGATE", False)
    router = _router()
    router._pregate = AsyncMock(side_effect=AssertionError("pregate must not run"))

    with patch.object(router, "_confidence_check", new=AsyncMock(return_value=(None, 0.0))):
        res = await router.route("anything")

    assert res.skills == []
    assert not res.needs_clarification
