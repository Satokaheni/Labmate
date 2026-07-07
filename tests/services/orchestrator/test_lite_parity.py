"""Behavioral PARITY REGRESSION GATE: graph (LangGraph) vs lite (`run_goal_lite`).

This is NOT the project decider between the two engines — behavior ties by
construction, since both ultimately call `AsyncOrchestrator.react_execute` for
actual work. It IS a regression net: if a future edit makes the two engines
silently diverge on a scenario lite is meant to reproduce, this test catches it.

Three scenarios (see docstrings below for exact assertions):
  1. single-skill execute            -> MUST TIE
  2. failing-goal reflect-retry loop -> MUST TIE
  3. trivial direct-answer fast-path -> KNOWN DIVERGENCE (xfail, documents the gap)

Design: plain-parametrized-style pytest (no pytest-bdd). Dual-engine-in-one-
scenario (build identical mocks, drive both engines, compare) reads more
directly as ordinary async test functions than as Gherkin steps — the BDD
layer would need step defs that thread two engines' state through one
scenario context, which added ceremony without adding clarity here. No
.feature file is included for this reason.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.orchestrator.coding_orchestrator import AsyncOrchestrator, CodingOrchestrator
from services.orchestrator.lite_orchestrator import run_goal_lite
from services.orchestrator.lite_state import build_initial_state
from services.orchestrator.skill_router import RouteResult

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Shared helpers — build identical mocked orchestrators for each engine and
# drive the scenario through it.
# ---------------------------------------------------------------------------


def _mock_router(skills: list[str], sub_intent: str) -> MagicMock:
    """A skill_router double whose route() mirrors what SkillRouter.route() returns
    (services/orchestrator/skill_router.py:226) for either a confident-skill match
    (single-skill execute) or a skill-less task (direct-answer fall-through)."""
    fake_router = MagicMock()
    fake_router.route = AsyncMock(
        return_value=RouteResult(skills=skills, needs_clarification=False, sub_intents=[sub_intent])
    )
    fake_router.runner.catalog_prompt.return_value = "CATALOG"
    return fake_router


async def _run_graph(
    task: str,
    architect_json: str,
    react_results,
    skills: list[str],
    monkeypatch,
    architect_side_effect_extra: list | None = None,
) -> dict:
    """Drive the real LangGraph `build_graph` engine on `task`, mocking only the
    architect() (assess_ambiguity JSON) and react_execute() (actual work) seams —
    exactly the two seams `run_goal_lite` mocks in test_lite_orchestrator.py.

    `react_results`: a single dict, or a list (side_effect) of dicts, matching the
    shape react_execute returns ({"ok", "summary", "tools_used", "tests_passed"}).
    A REAL AsyncOrchestrator is used (not a MagicMock) so plan_and_dispatch really
    drives _run_worker -> react_execute, exactly like production (mirrors the
    pattern in test_continuity_injection.py, which builds a real AsyncOrchestrator
    with a scripted completion seam)."""
    from langgraph.checkpoint.memory import MemorySaver

    from services.orchestrator import graph as graph_mod

    async def fake_emit(type, **fields):
        pass

    monkeypatch.setattr(graph_mod.events, "emit", fake_emit)

    fake_router = _mock_router(skills, task)

    mock_orch = MagicMock(spec=CodingOrchestrator)
    mock_orch.context_manager = None
    mock_orch.agent_instructions = ""
    if architect_side_effect_extra is not None:
        mock_orch.architect = AsyncMock(side_effect=[architect_json, *architect_side_effect_extra])
    else:
        mock_orch.architect = AsyncMock(return_value=architect_json)
    mock_orch.skill_router = fake_router

    async_orch = AsyncOrchestrator(skill_router=fake_router, mcp=None, max_steps=3)
    async_orch.context_manager = None
    if isinstance(react_results, list):
        async_orch.react_execute = AsyncMock(side_effect=react_results)
    else:
        async_orch.react_execute = AsyncMock(return_value=react_results)

    real_cp = MemorySaver()
    with patch("services.orchestrator.graph._make_sqlite_checkpointer", return_value=real_cp):
        graph, _ = graph_mod.build_graph(mock_orch, async_orch)

    initial_state = build_initial_state(task, "s-graph")
    config = {"configurable": {"thread_id": f"t-{hash(task) & 0xFFFF}"}}
    final_state = await graph.ainvoke(initial_state, config)
    return final_state


async def _run_lite(
    task: str,
    architect_json: str,
    react_results,
    architect_side_effect_extra: list | None = None,
) -> dict:
    """Drive `run_goal_lite` on `task`, mocking the identical two seams as
    `_run_graph` above (architect for assess_ambiguity, react_execute for work)."""
    orch = MagicMock()
    orch.context_manager = None
    if architect_side_effect_extra is not None:
        orch.architect = AsyncMock(side_effect=[architect_json, *architect_side_effect_extra])
    else:
        orch.architect = AsyncMock(return_value=architect_json)

    async_orch = MagicMock()
    if isinstance(react_results, list):
        async_orch.react_execute = AsyncMock(side_effect=react_results)
    else:
        async_orch.react_execute = AsyncMock(return_value=react_results)

    return await run_goal_lite(orch, async_orch, task, "s-lite")


LOW_AMBIGUITY_JSON = json.dumps({"assumptions": [], "ambiguity": 0.0, "blocking_question": ""})


# ---------------------------------------------------------------------------
# Scenario 1: single-skill execute (clear task, low ambiguity, ok=True) — MUST TIE
# ---------------------------------------------------------------------------


@pytest.mark.mocked
async def test_scenario1_single_skill_execute_ties(monkeypatch):
    """A clear task that confidently routes to ONE skill and succeeds on the first
    attempt must produce the SAME ok/tests_passed/final_answer on both engines."""
    task = "reverse a string in python"
    react_result = {
        "ok": True,
        "summary": "def reverse(s): return s[::-1]",
        "tools_used": ["write_file"],
        "tests_passed": True,
    }

    graph_state = await _run_graph(
        task, LOW_AMBIGUITY_JSON, react_result, skills=["code-gen"], monkeypatch=monkeypatch
    )
    lite_state = await _run_lite(task, LOW_AMBIGUITY_JSON, react_result)

    # Structural comparison (project rule: assert structure, not exact LLM text —
    # but here react_execute is MOCKED so the underlying summary IS the fixture
    # string, and both engines read the SAME mocked result). NOTE: graph's `check`
    # node wraps the summary as "**{goal description}**\n{summary}" (graph.py:486)
    # while lite's final_answer is the RAW summary — a real, if cosmetic, formatting
    # difference between the engines. The parity assertion is that both engines'
    # final_answer CONTAINS the identical underlying mocked summary, not that the
    # two strings are byte-identical.
    assert graph_state.get("final_answer")
    assert lite_state.get("final_answer")
    assert react_result["summary"] in graph_state["final_answer"]
    assert lite_state["final_answer"] == react_result["summary"]
    assert lite_state["ok"] is True
    assert lite_state["tests_passed"] is True
    # graph: root goal COMPLETED with no error is the graph's "ok" signal.
    assert graph_state["goal_tree"]["root"]["status"] == "COMPLETED"
    assert graph_state.get("error") is None
    assert graph_state.get("tests_passed") is True


# ---------------------------------------------------------------------------
# Scenario 2: failing-goal reflect-retry (ok=False then ok=True) — MUST TIE
# ---------------------------------------------------------------------------


@pytest.mark.mocked
async def test_scenario2_failing_goal_retries_then_ties(monkeypatch):
    """A goal that fails on attempt 1 and succeeds on attempt 2 must retry and
    finalize with the SAME terminal ok/tests_passed/final_answer on both engines.
    Graph retries via reflect -> router (FAILED + attempts < MAX_GOAL_ATTEMPTS);
    lite retries via its `for attempt in range(MAX_GOAL_ATTEMPTS)` loop."""
    task = "fix the bug in x.py"
    react_results = [
        {"ok": False, "summary": "attempt 1 failed", "tools_used": [], "tests_passed": False},
        {
            "ok": True,
            "summary": "attempt 2 fixed it",
            "tools_used": ["write_file"],
            "tests_passed": True,
        },
    ]
    diagnosis = "diagnosis: the fix was wrong; try a different approach"

    graph_state = await _run_graph(
        task,
        LOW_AMBIGUITY_JSON,
        list(react_results),
        skills=["code-gen"],
        monkeypatch=monkeypatch,
        architect_side_effect_extra=[diagnosis],
    )
    lite_state = await _run_lite(
        task, LOW_AMBIGUITY_JSON, list(react_results), architect_side_effect_extra=[diagnosis]
    )

    # Both engines retried exactly once (2 react_execute calls) and finalized ok=True.
    assert lite_state["ok"] is True
    assert lite_state["tests_passed"] is True
    assert lite_state["final_answer"] == "attempt 2 fixed it"

    assert graph_state["goal_tree"]["root"]["status"] == "COMPLETED"
    assert graph_state.get("error") is None
    assert graph_state.get("tests_passed") is True
    assert "attempt 2 fixed it" in graph_state.get("final_answer", "")


# ---------------------------------------------------------------------------
# Scenario 3: trivial direct-answer fast-path — KNOWN DIVERGENCE (xfail, documented)
# ---------------------------------------------------------------------------


@pytest.mark.mocked
@pytest.mark.xfail(
    reason=(
        "lite deferred the direct-answer fast-path; graph sets direct_answer=True, "
        "lite routes through react_execute — known spike limitation"
    ),
    strict=False,
)
async def test_scenario3_trivial_direct_answer_known_divergence(monkeypatch):
    """DOCUMENTS the honest gap rather than hiding it: for a trivial, skill-less
    task, graph's `plan` node takes the FIX-10 direct-answer fast-path (ONE
    architect() call, `direct_answer=True`, root COMPLETED, halts before
    execute/react_execute). lite has NO direct-answer fast-path (see the
    "# direct-answer fast-path deferred" note in lite_orchestrator.py) — it
    falls through to the react_execute-based execute loop for every non-halted
    task, low-ambiguity or not.

    This assertion is EXPECTED TO FAIL (xfail, non-strict): it asserts the two
    engines tie on `direct_answer`-truthiness AND on whether react_execute was
    invoked. If this ever unexpectedly PASSES (XPASS), that means lite grew (or
    graph lost) the direct-answer fast-path — a real convergence worth
    promoting out of xfail.
    """
    task = "what is 2+2?"

    graph_state = await _run_graph(
        task,
        LOW_AMBIGUITY_JSON,
        {"ok": True, "summary": "should not be reached"},
        skills=[],
        monkeypatch=monkeypatch,
    )

    orch = MagicMock()
    orch.context_manager = None
    orch.architect = AsyncMock(return_value=LOW_AMBIGUITY_JSON)
    async_orch = MagicMock()
    async_orch.react_execute = AsyncMock(
        return_value={"ok": True, "summary": "2 + 2 is 4.", "tools_used": [], "tests_passed": False}
    )
    lite_state = await run_goal_lite(orch, async_orch, task, "s-lite")

    # graph: direct-answer fast-path taken -> direct_answer truthy, react_execute
    # never called (plan node answered directly via architect() and halted).
    assert graph_state.get("direct_answer") is True

    # lite: NO direct-answer fast-path -> direct_answer is never set truthy, and
    # react_execute WAS called to produce the final answer.
    assert lite_state.get("direct_answer") is True  # <- EXPECTED TO FAIL: lite never sets this
    async_orch.react_execute.assert_not_called()  # <- EXPECTED TO FAIL: lite always calls it
