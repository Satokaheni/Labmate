"""Pre-flight calls must cap content length (routing-latency fix).

The ambiguity-triage and skill-routing model calls emit small structured output
(JSON / a load_skill tool call) but had NO max_tokens — so the model could ramble
to thousands of content tokens (~3.8k observed live), the dominant pre-answer latency.
These tests assert the cap is actually forwarded to the model call, and that
answer-generating calls (no cap) stay unbounded.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.mocked


def _resp(content: str = "{}"):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _coding_orch():
    """Minimal CodingOrchestrator instance to exercise architect() in isolation
    (architect only reads self._bases + self.agent_instructions)."""
    from services.orchestrator.coding_orchestrator import CodingOrchestrator

    orch = object.__new__(CodingOrchestrator)
    orch._bases = ["http://localhost:8000/v1"]
    orch.agent_instructions = ""
    return orch


@pytest.mark.asyncio
async def test_architect_forwards_max_tokens_when_set():
    """architect(max_tokens=N) must forward max_tokens=N to the model call (caps the JSON)."""
    orch = _coding_orch()
    with patch(
        "services.orchestrator.coding_orchestrator.acompletion_with_failover",
        new_callable=AsyncMock,
    ) as m:
        m.return_value = _resp('{"ambiguity": 0.0}')
        await orch.architect("triage this", thinking_budget=384, max_tokens=512)
    assert m.call_args.kwargs.get("max_tokens") == 512


@pytest.mark.asyncio
async def test_architect_omits_max_tokens_when_none():
    """Answer-generating calls (direct answer / reflect) pass no cap → stay unbounded."""
    orch = _coding_orch()
    with patch(
        "services.orchestrator.coding_orchestrator.acompletion_with_failover",
        new_callable=AsyncMock,
    ) as m:
        m.return_value = _resp("a long prose answer")
        await orch.architect("what is the traveling salesman problem?")
    assert "max_tokens" not in m.call_args.kwargs


@pytest.mark.asyncio
async def test_route_sample_select_caps_max_tokens():
    """The tool-calling routing sample must cap content at ROUTE_MAX_TOKENS."""
    from services.orchestrator.skill_router import ROUTE_MAX_TOKENS, SkillRouter

    runner = MagicMock()
    runner.catalog_prompt.return_value = "- some-skill: does a thing"
    runner.tool_schema.return_value = {"type": "function", "function": {"name": "load_skill"}}
    router = SkillRouter(
        runner=runner, redis=AsyncMock(), gemma_api_base="http://localhost:8000/v1"
    )

    with patch(
        "services.orchestrator.skill_router.litellm.acompletion", new_callable=AsyncMock
    ) as m:
        m.return_value = SimpleNamespace(choices=[])  # empty → _sample_select returns None
        await router._sample_select("do x", thinking_budget=0)
    assert m.call_args.kwargs.get("max_tokens") == ROUTE_MAX_TOKENS
