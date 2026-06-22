"""Regression tests for the check() node walking the FULL subtree.

After FIX 3, multi-intent sub-intents are nested as a CHAIN under root
(root -> subN -> ... -> sub1(leaf)), so root["children"] contains ONLY the
last sub-intent. The check() node must walk all transitive descendants of
root when finalizing, otherwise:
  (a) every nested sub-intent's result is silently dropped from the final
      answer (the user-facing regression flagged by the project review), and
  (b) finalization could fire as soon as the single direct child is terminal
      while deeper descendants are still PENDING (premature finalization).

These tests build the nested chain exactly as graph.plan() does (FIX 3) and
assert the final answer contains EVERY sub-intent result, in submission order.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from services.orchestrator.types import (
    Status,
    create_goal,
    update_status,
)
from services.orchestrator import events


def _make_state(**overrides) -> dict:
    tree: dict = {}
    create_goal(tree, "root", None, "search a dataset and generate examples")
    base = {
        "session_id": "test-subtree",
        "goal_tree": tree,
        "current_goal_id": "root",
        "step_markers": {},
        "messages": [],
        "error": None,
        "root_goal": "search a dataset and generate examples",
        "last_artifact": {"type": "other", "payload": ""},
        "verified": False,
        "critique_score": 1.0,
        "critique_notes": "",
    }
    base.update(overrides)
    return base


def _build_nested_chain(tree: dict, sub_intents: list[str]) -> list[str]:
    """Build the FIX-3 nested chain root -> subN -> ... -> sub1(leaf).

    Returns the child goal ids in CREATION order (matching reversed(sub_intents)),
    i.e. [subN_id, ..., sub1_id].
    """
    prev_id = "root"
    ids: list[str] = []
    # plan() iterates reversed(sub_intents): the LAST sub-intent becomes root's
    # direct child, the FIRST sub-intent becomes the deepest leaf.
    for i, sub_intent in enumerate(reversed(sub_intents)):
        cid = f"sub_{i}"
        create_goal(tree, cid, prev_id, sub_intent)
        ids.append(cid)
        prev_id = cid
    return ids


@pytest.fixture
def fake_event_emitter(monkeypatch):
    captured: list[tuple[str, dict]] = []

    async def fake_emit(type, **fields):
        captured.append((type, fields))

    monkeypatch.setattr(events, "emit", fake_emit)
    return captured


def _check_node():
    from services.orchestrator.graph import make_nodes
    from services.orchestrator.coding_orchestrator import (
        CodingOrchestrator,
        AsyncOrchestrator,
    )

    mock_orch = MagicMock(spec=CodingOrchestrator)
    mock_async_orch = MagicMock(spec=AsyncOrchestrator)
    _, _, check_node, _, _, _, _ = make_nodes(mock_orch, mock_async_orch)
    return check_node


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_check_includes_all_nested_subintent_results(fake_event_emitter):
    """REGRESSION: the final answer must include EVERY sub-intent result, not just
    root's single direct child. This is the exact scenario from the project review:
    both child goals COMPLETED but only the first appeared in the final answer."""
    check_node = _check_node()
    state = _make_state()
    tree = state["goal_tree"]

    # FIX-3 chain: root -> sub_0("generate examples") -> sub_1("search a dataset")
    chain_ids = _build_nested_chain(
        tree, ["search a dataset", "generate examples"]
    )
    # sub_1 is the deepest leaf == "search a dataset"; sub_0 is root's direct child
    # == "generate examples". Both COMPLETED.
    for cid in chain_ids:
        desc = tree[cid]["description"]
        update_status(tree, cid, Status.COMPLETED, result=f"RESULT:{desc}")

    delta = await check_node(state)

    assert "final_answer" in delta
    answer = delta["final_answer"]
    # BOTH results must be present (the bug dropped "generate examples").
    assert "RESULT:search a dataset" in answer
    assert "RESULT:generate examples" in answer
    assert "**search a dataset**" in answer
    assert "**generate examples**" in answer
    # Submission order: first sub-intent (deepest leaf) appears before the last.
    assert answer.index("search a dataset") < answer.index("generate examples")
    # Root finalized COMPLETED (no failures).
    assert delta["goal_tree"]["root"]["status"] == Status.COMPLETED.value


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_check_does_not_finalize_when_nested_descendant_pending(
    fake_event_emitter,
):
    """REGRESSION: with only root's direct child terminal but a deeper descendant
    still PENDING, check() must NOT finalize (premature finalization guard)."""
    check_node = _check_node()
    state = _make_state()
    tree = state["goal_tree"]

    chain_ids = _build_nested_chain(
        tree, ["search a dataset", "generate examples"]
    )
    # sub_0 = root's direct child ("generate examples") COMPLETED, but
    # sub_1 = deepest leaf ("search a dataset") still PENDING.
    direct_child = chain_ids[0]
    leaf = chain_ids[1]
    update_status(tree, direct_child, Status.COMPLETED, result="RESULT:generate examples")
    assert tree[leaf]["status"] == Status.PENDING.value

    delta = await check_node(state)

    # Must NOT finalize: no final_answer, root not COMPLETED.
    assert "final_answer" not in delta
    assert tree["root"]["status"] != Status.COMPLETED.value


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_check_surfaces_nested_failed_descendant(fake_event_emitter):
    """A failed nested descendant (attempts exhausted) must surface in the final
    answer and mark root FAILED, even though it is not a direct child of root."""
    check_node = _check_node()
    state = _make_state()
    tree = state["goal_tree"]

    chain_ids = _build_nested_chain(
        tree, ["search a dataset", "generate examples"]
    )
    direct_child = chain_ids[0]  # "generate examples"
    leaf = chain_ids[1]          # "search a dataset" (deepest)
    update_status(tree, direct_child, Status.COMPLETED, result="RESULT:generate examples")
    update_status(tree, leaf, Status.FAILED, error="dataset API unreachable")
    tree[leaf]["attempts"] = 3  # exhausted -> not retryable

    delta = await check_node(state)

    assert "final_answer" in delta
    answer = delta["final_answer"]
    assert "RESULT:generate examples" in answer
    assert "Failed subtasks:" in answer
    assert "search a dataset" in answer
    assert delta["goal_tree"]["root"]["status"] == Status.FAILED.value
    assert "error" in delta


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_check_defers_to_reflect_for_nested_retryable_failure(
    fake_event_emitter,
):
    """A nested (non-direct-child) failed descendant with attempts < 3 must defer
    finalization and route to reflect via current_goal_id."""
    check_node = _check_node()
    state = _make_state()
    tree = state["goal_tree"]

    chain_ids = _build_nested_chain(
        tree, ["search a dataset", "generate examples"]
    )
    direct_child = chain_ids[0]
    leaf = chain_ids[1]
    update_status(tree, direct_child, Status.COMPLETED, result="RESULT:generate examples")
    update_status(tree, leaf, Status.FAILED, error="transient")
    tree[leaf]["attempts"] = 1  # retryable

    delta = await check_node(state)

    assert "final_answer" not in delta
    # Must route to the nested failed leaf, which root["children"] alone would miss.
    assert delta.get("current_goal_id") == leaf
