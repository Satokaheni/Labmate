"""FIX 8: assess_ambiguity rubric refinement.

NEW file only. The actual calibration QUALITY (what float a live model returns)
is validated by a human-run live battery, NOT here — these tests would be brittle
if they asserted exact scores. Instead these are mechanism-level checks:

  1. The prompt sent to architect() now teaches the core-vs-minor distinction:
     it states that unstated MINOR execution parameters (format, count, library,
     styling) are NOT ambiguity, and it includes the previously-borderline
     "Hugging Face Hub ... emotion classification datasets" example as a LOW case.
  2. The node still parses the model's JSON and does NOT halt when the model
     scores the (now-correctly-calibrated) HF-search task LOW.

All tests are mocked (no GPU / no services).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.orchestrator import events


class _FakeEmitter:
    def __init__(self):
        self.events = []

    async def emit(self, type: str, **fields):
        self.events.append((type, fields))


@pytest.fixture
def fake_emitter():
    emitter = _FakeEmitter()
    token = events.current_emitter.set(emitter)
    yield emitter
    events.current_emitter.reset(token)


def _make_state(root_goal: str) -> dict:
    from services.orchestrator.types import create_goal

    tree: dict = {}
    create_goal(tree, "root", None, root_goal)
    return {
        "session_id": "test-fix8-001",
        "goal_tree": tree,
        "current_goal_id": "root",
        "step_markers": {},
        "messages": [],
        "error": None,
        "root_goal": root_goal,
    }


def _build_assess():
    from services.orchestrator.graph import make_nodes
    from services.orchestrator.coding_orchestrator import (
        CodingOrchestrator,
        AsyncOrchestrator,
    )

    mock_orch = MagicMock(spec=CodingOrchestrator)
    mock_orch.architect = AsyncMock(
        return_value='{"assumptions": [], "ambiguity": 0.1, "blocking_question": ""}'
    )
    mock_async_orch = MagicMock(spec=AsyncOrchestrator)
    nodes = make_nodes(mock_orch, mock_async_orch)
    return nodes[5], mock_orch


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_prompt_teaches_core_vs_minor_distinction(fake_emitter):
    assess, mock_orch = _build_assess()

    await assess(_make_state("search the Hugging Face Hub for emotion classification datasets"))

    # architect() received the calibrated rubric.
    prompt = mock_orch.architect.await_args.args[0]
    lower = prompt.lower()
    # Core objective is the thing scored, not every execution detail.
    assert "core" in lower
    # Minor parameters are explicitly excluded from raising the score.
    assert "minor" in lower
    assert "default" in lower
    assert "format" in lower and "count" in lower
    # The previously-borderline HF example is taught as a LOW case.
    assert "hugging face" in lower


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_well_specified_search_does_not_halt(fake_emitter):
    """A clear objective with only unstated minor params (format/count) parses
    LOW and does NOT trigger the clarification halt."""
    assess, _ = _build_assess()

    delta = await assess(
        _make_state("search the Hugging Face Hub for emotion classification datasets")
    )

    assert delta["ambiguity"] == 0.1
    assert "awaiting_clarification" not in delta
    assert "clarification_question" not in delta
    halts = [f for name, f in fake_emitter.events if name == "clarification_request"]
    assert halts == []
