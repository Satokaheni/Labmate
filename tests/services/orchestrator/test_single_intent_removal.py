"""Post-removal invariants for the multi-intent decompose / routing_mode A/B removal.

A broadened A/B concluded the multi-intent DECOMPOSE path added cost + flakiness with no
quality benefit, so the decompose machinery (decompose(), _generate_clarification(), the
ROUTING_MODE constant + routing_mode State field + per-request mode toggle) was removed and
single-intent routing was hardwired as the only behavior.

These tests (NEW file) pin the invariants the removal must preserve:
  - route() is single-intent: a confident skill -> skills=[skill], sub_intents=[task];
    no confident skill -> skills=[], needs_clarification False (direct-answer fall-through).
  - route() NEVER clarifies (the assess_ambiguity node owns ambiguity).
  - route() takes NO mode kwarg.
  - the plan node creates AT MOST one child goal, or direct-answers.
  - assess_ambiguity still clarifies + halts on genuine high ambiguity.
  - none of the removed symbols (ROUTING_MODE / routing_mode / decompose /
    _generate_clarification) remain referenced in the orchestrator non-test code.

All mocked (no GPU / no services).
"""
from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_router():
    from services.orchestrator.skill_router import SkillRouter

    runner = MagicMock()
    runner.catalog = {"dataset-search": "find datasets"}
    runner.catalog_prompt.return_value = "CATALOG"
    runner.tool_schema.return_value = {"type": "function", "function": {"name": "load_skill"}}
    redis = MagicMock()
    redis.xadd = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    return SkillRouter(runner, redis, "http://test/v1")


# ── route() single-intent invariants ───────────────────────────────────────


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_route_confident_skill_returns_one_intent_result(monkeypatch):
    router = _make_router()
    monkeypatch.setattr(
        router, "_confidence_check", AsyncMock(return_value=("dataset-search", 1.0))
    )
    result = await router.route("find an emotion dataset")
    assert result.skills == ["dataset-search"]
    assert result.sub_intents == ["find an emotion dataset"]
    assert result.needs_clarification is False


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_route_no_skill_returns_empty_no_clarification(monkeypatch):
    router = _make_router()
    monkeypatch.setattr(
        router, "_confidence_check", AsyncMock(return_value=(None, 0.0))
    )
    result = await router.route("What is 2+2?")
    assert result.skills == []
    assert result.needs_clarification is False
    assert result.sub_intents == ["What is 2+2?"]


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_route_never_clarifies_even_below_threshold(monkeypatch):
    """A below-CONFIDENCE_THRESHOLD match is NOT a confident skill and is NOT ambiguity:
    route() falls through to direct-answer (skills=[], needs_clarification False)."""
    router = _make_router()
    monkeypatch.setattr(
        router, "_confidence_check", AsyncMock(return_value=("dataset-search", 1 / 3))
    )
    result = await router.route("do an ambiguous thing")
    assert result.skills == []
    assert result.needs_clarification is False


@pytest.mark.mocked
def test_route_takes_no_mode_param():
    from services.orchestrator.skill_router import SkillRouter

    sig = inspect.signature(SkillRouter.route)
    assert "mode" not in sig.parameters
    # task + self only.
    assert list(sig.parameters) == ["self", "task"]


@pytest.mark.mocked
def test_removed_symbols_are_gone():
    from services.orchestrator import skill_router as sr_mod

    assert not hasattr(sr_mod, "ROUTING_MODE")
    assert not hasattr(sr_mod.SkillRouter, "decompose")
    assert not hasattr(sr_mod.SkillRouter, "_generate_clarification")

    from services.orchestrator.types import State

    assert "routing_mode" not in State.__annotations__


# ── plan node: at most one child goal, or direct-answer ─────────────────────


def _plan_state(desc: str = "do a thing") -> dict:
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


def _make_orch(architect_return="ANSWER"):
    from services.orchestrator.coding_orchestrator import CodingOrchestrator
    orch = MagicMock(spec=CodingOrchestrator)
    orch.architect = AsyncMock(return_value=architect_return)
    return orch


def _make_async_orch():
    from services.orchestrator.coding_orchestrator import AsyncOrchestrator
    return MagicMock(spec=AsyncOrchestrator)


@pytest.fixture(autouse=True)
def _silence_events(monkeypatch):
    from services.orchestrator import graph as graph_mod

    async def fake_emit(_type, **_fields):
        pass

    monkeypatch.setattr(graph_mod.events, "emit", fake_emit)


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_plan_creates_at_most_one_child_goal_on_skill():
    from services.orchestrator import graph as graph_mod
    from services.orchestrator.skill_router import RouteResult

    route_result = RouteResult(
        skills=["dataset-search"], needs_clarification=False, sub_intents=["find a dataset"]
    )
    fake_router = MagicMock()
    fake_router.route = AsyncMock(return_value=route_result)
    fake_router.runner.catalog_prompt.return_value = "CATALOG"

    orch = _make_orch()
    orch.skill_router = fake_router

    plan_node, *_ = graph_mod.make_nodes(orch, _make_async_orch())
    out = await plan_node(_plan_state("find a dataset"))

    non_root = [g for g in out["goal_tree"] if g != "root"]
    assert len(non_root) == 1
    assert out["goal_tree"]["root"]["children"] == non_root
    # No direct answer when a skill matched.
    assert "final_answer" not in out
    orch.architect.assert_not_called()


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_plan_direct_answers_when_no_skill():
    from services.orchestrator import graph as graph_mod
    from services.orchestrator.skill_router import RouteResult

    route_result = RouteResult(skills=[], needs_clarification=False, sub_intents=["x"])
    fake_router = MagicMock()
    fake_router.route = AsyncMock(return_value=route_result)
    fake_router.runner.catalog_prompt.return_value = "CATALOG"

    orch = _make_orch(architect_return="The answer is 4.")
    orch.skill_router = fake_router

    plan_node, *_ = graph_mod.make_nodes(orch, _make_async_orch())
    out = await plan_node(_plan_state("What is 2+2?"))

    assert out.get("direct_answer") is True
    assert out.get("final_answer") == "The answer is 4."
    # No child goals created — direct answer halts the graph.
    assert out["goal_tree"]["root"]["children"] == []
    assert len(out["goal_tree"]) == 1


# ── make_nodes still returns exactly 7 nodes in order ───────────────────────


@pytest.mark.mocked
def test_make_nodes_returns_seven_nodes_in_order():
    from services.orchestrator.graph import make_nodes

    nodes = make_nodes(_make_orch(), _make_async_orch())
    assert len(nodes) == 7
    names = [n.__name__ for n in nodes]
    assert names == [
        "plan", "execute_node", "check", "reflect",
        "approval", "assess_ambiguity", "verify",
    ]


# ── assess_ambiguity still clarifies + halts on genuine high ambiguity ──────


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_assess_ambiguity_still_clarifies_high_ambiguity():
    from services.orchestrator import graph as graph_mod

    orch = _make_orch(
        architect_return='{"assumptions": [], "ambiguity": 0.9, '
        '"blocking_question": "What should I improve?"}'
    )
    _, _, _, _, _, assess_node, _ = graph_mod.make_nodes(orch, _make_async_orch())

    state = _plan_state("make it better")
    state["root_goal"] = "make it better"
    out = await assess_node(state)

    assert out["ambiguity"] >= graph_mod.AMBIGUITY_THRESHOLD
    assert out.get("awaiting_clarification") is True
    assert out.get("clarification_question") == "What should I improve?"
    # ambiguity_router halts on this.
    assert graph_mod.ambiguity_router(out) == graph_mod.END


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_assess_ambiguity_proceeds_on_low_ambiguity():
    from services.orchestrator import graph as graph_mod

    orch = _make_orch(
        architect_return='{"assumptions": [], "ambiguity": 0.1, "blocking_question": ""}'
    )
    _, _, _, _, _, assess_node, _ = graph_mod.make_nodes(orch, _make_async_orch())

    state = _plan_state("reverse a string in python")
    state["root_goal"] = "reverse a string in python"
    out = await assess_node(state)

    assert out.get("awaiting_clarification") is not True
    assert graph_mod.ambiguity_router(out) == "plan"


# ── no dead references to removed symbols in orchestrator non-test code ──────


@pytest.mark.mocked
def test_no_dead_references_in_orchestrator_source():
    """The orchestrator package must not REFERENCE the removed symbols in code
    (comments / docstrings explaining the removal are allowed)."""
    import re
    import services.orchestrator as pkg

    pkg_dir = Path(pkg.__file__).resolve().parent
    # Code-shaped patterns (not bare prose words) so docstrings/comments that EXPLAIN
    # the removal don't false-positive. These match how the symbols would actually be
    # USED in code.
    forbidden = [
        r"\bROUTING_MODE\b",            # the removed module constant (any code use)
        r"\.decompose\(",              # method call
        r"_generate_clarification",     # removed method (any reference)
        r"routing_mode\s*[:=]",        # routing_mode= kwarg / param / dict key assign
        r"""\[['"]routing_mode['"]\]""",  # state["routing_mode"]
        r"""\.get\(\s*['"]routing_mode['"]""",  # state.get("routing_mode")
    ]
    patt = re.compile("|".join(forbidden))
    offenders: list[str] = []
    for py in pkg_dir.rglob("*.py"):
        for lineno, line in enumerate(py.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if patt.search(code):
                offenders.append(f"{py.name}:{lineno}: {line.strip()}")
    assert not offenders, "removed symbols still referenced in code:\n" + "\n".join(offenders)
