"""Unit tests for the opt-in `revise` graph node and the check->revise->END wiring.

The revise node is the only place that makes the (bounded) revision model call.
orch.architect is mocked — no real inference.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

import services.orchestrator.graph as graph_mod
from services.orchestrator.graph import make_nodes, _run_had_side_effects


@pytest.fixture(autouse=True)
def _enable_feature(monkeypatch):
    """Default the feature ON for these tests by re-reading the env knobs.

    graph.py reads ENABLE_FINALIZE_REVISION at import time into a module global,
    so set the env AND patch the already-bound module globals.
    """
    monkeypatch.setenv("ENABLE_FINALIZE_REVISION", "1")
    monkeypatch.setattr(graph_mod, "ENABLE_FINALIZE_REVISION", True, raising=False)
    monkeypatch.setattr(graph_mod, "MAX_FINALIZE_REVISIONS", 1, raising=False)


def _nodes(architect):
    orch = MagicMock()
    orch.architect = architect
    async_orch = MagicMock()
    # make_nodes returns an 8-tuple ending with the revise node (Task 3).
    return make_nodes(orch, async_orch)


def _finalized_state(answer="2, 3, 5", task="List primes under 10", **extra):
    state = {
        "root_goal": task,
        "final_answer": answer,
        "last_artifact": {"type": "other", "payload": answer},
        "finalize_revisions": 0,
    }
    state.update(extra)
    return state


@pytest.mark.asyncio
async def test_revise_node_revises_once_when_enabled(monkeypatch):
    monkeypatch.setattr(graph_mod.events, "emit", AsyncMock())
    architect = AsyncMock(return_value="2, 3, 5, 7")
    nodes = _nodes(architect)
    revise = nodes[7]

    out = await revise(_finalized_state())

    architect.assert_awaited_once()
    assert out["final_answer"] == "2, 3, 5, 7"
    assert out["finalize_revisions"] == 1
    assert out["revised"] is True


@pytest.mark.asyncio
async def test_revise_node_passthrough_when_disabled(monkeypatch):
    monkeypatch.setattr(graph_mod, "ENABLE_FINALIZE_REVISION", False, raising=False)
    monkeypatch.setattr(graph_mod.events, "emit", AsyncMock())
    architect = AsyncMock(return_value="SHOULD NOT BE CALLED")
    nodes = _nodes(architect)
    revise = nodes[7]

    out = await revise(_finalized_state())

    architect.assert_not_awaited()
    assert out == {}  # no state change -> identical delivery to today


@pytest.mark.asyncio
async def test_revise_node_respects_cap(monkeypatch):
    monkeypatch.setattr(graph_mod.events, "emit", AsyncMock())
    architect = AsyncMock(return_value="2, 3, 5, 7")
    nodes = _nodes(architect)
    revise = nodes[7]

    out = await revise(_finalized_state(finalize_revisions=1))

    architect.assert_not_awaited()
    assert out == {}


@pytest.mark.asyncio
async def test_revise_node_skips_after_side_effects(monkeypatch):
    monkeypatch.setattr(graph_mod.events, "emit", AsyncMock())
    architect = AsyncMock(return_value="rewritten")
    nodes = _nodes(architect)
    revise = nodes[7]

    state = _finalized_state(
        answer="Created report.txt.",
        last_artifact={"type": "code", "payload": "wrote file"},
    )
    out = await revise(state)

    architect.assert_not_awaited()
    assert out == {}


@pytest.mark.asyncio
async def test_revise_node_skips_when_errored(monkeypatch):
    monkeypatch.setattr(graph_mod.events, "emit", AsyncMock())
    architect = AsyncMock(return_value="rewritten")
    nodes = _nodes(architect)
    revise = nodes[7]

    out = await revise(_finalized_state(error="1 subtask(s) failed"))

    architect.assert_not_awaited()
    assert out == {}


@pytest.mark.asyncio
async def test_revise_node_skips_when_no_visible_answer(monkeypatch):
    monkeypatch.setattr(graph_mod.events, "emit", AsyncMock())
    architect = AsyncMock(return_value="rewritten")
    nodes = _nodes(architect)
    revise = nodes[7]

    out = await revise(_finalized_state(answer="   "))

    architect.assert_not_awaited()
    assert out == {}


# ── _run_had_side_effects: conservative side-effect signal ───────────────────
def test_had_side_effects_true_for_code_artifact():
    assert _run_had_side_effects({"last_artifact": {"type": "code", "payload": "x"}}) is True


def test_had_side_effects_false_for_plain_text_artifact():
    assert _run_had_side_effects({"last_artifact": {"type": "other", "payload": "x"}}) is False


def test_had_side_effects_false_for_writing_artifact():
    # 'writing' is a long prose answer, not a side effect.
    assert _run_had_side_effects({"last_artifact": {"type": "writing", "payload": "x"}}) is False


def test_had_side_effects_false_when_absent():
    assert _run_had_side_effects({}) is False
