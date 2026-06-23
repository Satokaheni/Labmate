"""FIX 11 — route() must NOT clarify based on skill-absence.

Skill-match is not ambiguity. A MULTI-intent task whose sub-intents are perfectly
CLEAR but do not map to a confident skill (e.g. "write a Python function that reverses
a string, and write a pytest unit test for it") must PROCEED so every sub-intent becomes
a goal that ReAct handles at execute time — NOT get interrogated with a needless
clarifying question.

The dedicated assess_ambiguity gate (which runs BEFORE route) remains the sole owner of
clarification for GENUINE ambiguity. These tests pin:
  - route() never sets needs_clarification on skill-absence (one flagged or all flagged),
  - the plan node builds an N-goal sequential chain for a non-clarifying multi-intent
    result even when route_result.skills is empty (no clarification_request,
    awaiting_clarification not set),
  - regressions: assess_ambiguity still halts+clarifies on genuine ambiguity; the
    single-intent skill-less direct-answer fast-path is unchanged; routing_mode "single"
    is unchanged.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── shared doubles (mirrors test_skill_router_multi_intent.py) ──────────────

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


# ── route()-level: skill-absence -> PROCEED, never clarify ─────────────────

@pytest.mark.asyncio
async def test_route_multi_intent_one_flagged_proceeds_no_clarification():
    """Multi-intent decompose with ONE sub-intent flagged (skill None) -> proceed:
    needs_clarification False, both sub_intents kept, _generate_clarification NOT awaited."""
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch.object(router, "decompose",
                      AsyncMock(return_value=[
                          "reverse a python string",
                          "write a pytest unit test for it",
                      ])), \
         patch.object(router, "_confidence_check",
                      AsyncMock(side_effect=[
                          ("dataset-search", 1.0),   # confidently resolved
                          (None, 0.0),               # clear but skill-less
                      ])), \
         patch.object(router, "_generate_clarification",
                      AsyncMock(return_value="should never be called")) as gc:
        result = await router.route("reverse a string and write a pytest test")

    assert result.needs_clarification is False
    # Partial skills returned; the flagged sub-intent is re-resolved by ReAct at exec time.
    assert result.skills == ["dataset-search"]
    assert result.sub_intents == [
        "reverse a python string",
        "write a pytest unit test for it",
    ]
    gc.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_multi_intent_all_flagged_proceeds_no_clarification():
    """Multi-intent where ALL sub-intents are skill-less -> proceed (no clarification),
    skills empty, all sub_intents kept, _generate_clarification NOT awaited."""
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch.object(router, "decompose",
                      AsyncMock(return_value=[
                          "reverse a python string",
                          "write a pytest unit test for it",
                      ])), \
         patch.object(router, "_confidence_check",
                      AsyncMock(side_effect=[(None, 0.0), (None, 0.0)])), \
         patch.object(router, "_generate_clarification",
                      AsyncMock(return_value="should never be called")) as gc:
        result = await router.route("reverse a string and write a pytest test")

    assert result.needs_clarification is False
    assert result.skills == []
    assert result.sub_intents == [
        "reverse a python string",
        "write a pytest unit test for it",
    ]
    gc.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_never_returns_needs_clarification():
    """route() no longer returns needs_clarification=True for ANY input — single
    skill-less, multi all-flagged, and multi confident all yield False."""
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")

    # Single skill-less intent.
    with patch.object(router, "decompose", AsyncMock(return_value=["one clear thing"])), \
         patch.object(router, "_confidence_check", AsyncMock(return_value=(None, 0.0))):
        r1 = await router.route("one clear thing")
    assert r1.needs_clarification is False

    # Multi all-flagged.
    with patch.object(router, "decompose", AsyncMock(return_value=["a", "b"])), \
         patch.object(router, "_confidence_check",
                      AsyncMock(side_effect=[(None, 0.0), (None, 0.0)])):
        r2 = await router.route("a and b")
    assert r2.needs_clarification is False

    # Multi all-confident.
    with patch.object(router, "decompose", AsyncMock(return_value=["a", "b"])), \
         patch.object(router, "_confidence_check",
                      AsyncMock(side_effect=[("dataset-search", 1.0), ("synthetic-gen", 1.0)])):
        r3 = await router.route("a and b")
    assert r3.needs_clarification is False


# ── plan node: non-clarifying multi-intent builds a chain even with no skills ─

def _state(desc: str) -> dict:
    return {
        "session_id": "s1",
        "goal_tree": {
            "root": {
                "id": "root", "parent_id": None, "children": [],
                "description": desc,
                "status": "PENDING", "result": None, "error": None,
                "attempts": 0, "started_at": None, "updated_at": None,
            }
        },
        "current_goal_id": "root",
    }


def _make_orch(architect_return: str = "ok"):
    from services.orchestrator.coding_orchestrator import CodingOrchestrator
    orch = MagicMock(spec=CodingOrchestrator)
    orch.architect = AsyncMock(return_value=architect_return)
    return orch


def _make_async_orch():
    from services.orchestrator.coding_orchestrator import AsyncOrchestrator
    return MagicMock(spec=AsyncOrchestrator)


@pytest.fixture
def _capture_events(monkeypatch):
    from services.orchestrator import graph as graph_mod
    captured: list[tuple[str, dict]] = []

    async def fake_emit(type, **fields):
        captured.append((type, fields))

    monkeypatch.setattr(graph_mod.events, "emit", fake_emit)
    return captured


@pytest.mark.asyncio
async def test_plan_builds_chain_for_skill_less_multi_intent(_capture_events):
    """A non-clarifying multi-intent route_result with >=2 sub_intents but EMPTY skills
    builds a chain of N goals (every sub-intent in the tree) and does NOT emit
    clarification_request or set awaiting_clarification."""
    from services.orchestrator import graph as graph_mod
    from services.orchestrator.skill_router import RouteResult

    route_result = RouteResult(
        skills=[],            # skill-less but clear
        needs_clarification=False,
        sub_intents=[
            "reverse a python string",
            "write a pytest unit test for it",
        ],
    )
    fake_router = MagicMock()
    fake_router.route = AsyncMock(return_value=route_result)
    fake_router.runner.catalog_prompt.return_value = "CATALOG"

    orch = _make_orch()
    orch.skill_router = fake_router

    plan_node, *_ = graph_mod.make_nodes(orch, _make_async_orch())
    out = await plan_node(_state("reverse a string and write a pytest test"))

    # No clarification: did not pause, did not ask.
    assert out.get("awaiting_clarification") is False
    types_emitted = [t for t, _ in _capture_events]
    assert "clarification_request" not in types_emitted
    assert "clarification_question" not in out
    assert "final_answer" not in out  # not the single-intent direct-answer fast-path

    # A chain of 2 goals was built (root + 2 children), all sub_intents present.
    tree = out["goal_tree"]
    descs = {g["description"] for g in tree.values()}
    assert "reverse a python string" in descs
    assert "write a pytest unit test for it" in descs
    # root has exactly one direct child (chain, not parallel fan-out).
    assert len(tree["root"]["children"]) == 1
    # Total nodes: root + 2 sub-intents.
    assert len(tree) == 3
    # Chain invariant: every non-root node's parent_id agrees with its parent's children[].
    for gid, g in tree.items():
        if g["parent_id"] is not None:
            assert gid in tree[g["parent_id"]]["children"]
    # architect() was NOT called (no architect-decompose fall-through).
    orch.architect.assert_not_called()


@pytest.mark.asyncio
async def test_plan_builds_chain_for_partial_skill_multi_intent(_capture_events):
    """Partial skills (some resolved, some not) with >=2 sub_intents -> chain of N goals
    over the sub_intents (not the skills), no clarification."""
    from services.orchestrator import graph as graph_mod
    from services.orchestrator.skill_router import RouteResult

    route_result = RouteResult(
        skills=["dataset-search"],   # only one of two sub-intents resolved
        needs_clarification=False,
        sub_intents=["search a dataset", "write a custom analysis function"],
    )
    fake_router = MagicMock()
    fake_router.route = AsyncMock(return_value=route_result)
    fake_router.runner.catalog_prompt.return_value = "CATALOG"

    orch = _make_orch()
    orch.skill_router = fake_router

    plan_node, *_ = graph_mod.make_nodes(orch, _make_async_orch())
    out = await plan_node(_state("search a dataset and write a custom analysis function"))

    assert out.get("awaiting_clarification") is False
    assert "clarification_request" not in [t for t, _ in _capture_events]
    tree = out["goal_tree"]
    descs = {g["description"] for g in tree.values()}
    assert descs == {
        "search a dataset and write a custom analysis function",  # root
        "search a dataset",
        "write a custom analysis function",
    }
    assert len(tree) == 3
    orch.architect.assert_not_called()


# ── regressions: the dedicated ambiguity gate + the other paths are untouched ─

@pytest.mark.asyncio
async def test_assess_ambiguity_still_halts_on_genuine_ambiguity(monkeypatch):
    """The assess_ambiguity gate (the SOLE clarification path) still halts and asks on a
    genuinely ambiguous task. This is unchanged by FIX 11."""
    from services.orchestrator import graph as graph_mod
    from services.orchestrator.coding_orchestrator import CodingOrchestrator

    captured: list[tuple[str, dict]] = []

    async def fake_emit(type, **fields):
        captured.append((type, fields))

    monkeypatch.setattr(graph_mod.events, "emit", fake_emit)

    orch = MagicMock(spec=CodingOrchestrator)
    # High ambiguity -> assess_ambiguity must request clarification and halt.
    orch.architect = AsyncMock(
        return_value='{"assumptions": [], "ambiguity": 0.95, '
                     '"blocking_question": "What should I make better?"}'
    )

    nodes = graph_mod.make_nodes(orch, _make_async_orch())
    # make_nodes order: plan, execute_node, check, reflect, approval, assess_ambiguity, verify
    assert len(nodes) == 7
    assess_ambiguity = nodes[5]

    out = await assess_ambiguity(_state("Make it better."))

    # The genuine-ambiguity clarification path must still fire.
    assert out.get("awaiting_clarification") is True
    types_emitted = [t for t, _ in captured]
    assert "clarification_request" in types_emitted


@pytest.mark.asyncio
async def test_single_intent_skill_less_still_direct_answers(_capture_events):
    """Regression: a single skill-less intent still uses the direct-answer fast-path
    (Fix 5/10) — NOT the chain, NOT clarification."""
    from services.orchestrator import graph as graph_mod
    from services.orchestrator.skill_router import RouteResult

    route_result = RouteResult(skills=[], needs_clarification=False, sub_intents=["what is 2+2"])
    fake_router = MagicMock()
    fake_router.route = AsyncMock(return_value=route_result)
    fake_router.runner.catalog_prompt.return_value = "CATALOG"

    orch = _make_orch(architect_return="The answer is 4.")
    orch.skill_router = fake_router

    plan_node, *_ = graph_mod.make_nodes(orch, _make_async_orch())
    out = await plan_node(_state("What is 2+2?"))

    assert out.get("direct_answer") is True
    assert out.get("final_answer") == "The answer is 4."
    # No chain: only root, COMPLETED, no children.
    tree = out["goal_tree"]
    assert tree["root"]["status"] == "COMPLETED"
    assert tree["root"]["children"] == []
    assert len(tree) == 1
    assert "clarification_request" not in [t for t, _ in _capture_events]


@pytest.mark.asyncio
async def test_routing_mode_single_unchanged():
    """Regression: mode='single' skips decompose (sub_intents=[task]); a skill-less single
    intent still proceeds with needs_clarification False and no clarifier call."""
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch.object(router, "decompose", AsyncMock()) as decompose, \
         patch.object(router, "_confidence_check", AsyncMock(return_value=(None, 0.0))), \
         patch.object(router, "_generate_clarification",
                      AsyncMock(return_value="should never be called")) as gc:
        result = await router.route("do everything", mode="single")

    # decompose() is skipped entirely in single mode.
    decompose.assert_not_awaited()
    assert result.sub_intents == ["do everything"]
    assert result.needs_clarification is False
    gc.assert_not_awaited()
