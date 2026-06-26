# tests/services/orchestrator/test_graph_fix9_retry_bounds.py
"""
FIX 9 — bound reflect/verify-gate latency with env-configurable knobs.

Covers:
  (a) verify gate caps verify->reflect at MAX_VERIFY_RETRIES, then accepts (routes to check);
      with MAX_VERIFY_RETRIES=0 it never reflects.
  (b) classify_error + is_terminal classifies environmental failures as terminal, others as retryable.
  (c) execute_node marks a non-retryable FAILED goal as exhausted (attempts == MAX_GOAL_ATTEMPTS)
      so check() finalizes with the error and does NOT route to reflect.
  (d) a retryable FAILED goal still increments attempts by 1 and is reflect-retried while
      attempts < MAX_GOAL_ATTEMPTS.
  (e) reflect uses REFLECT_THINKING_BUDGET for its architect() call.

These live in a NEW file; existing tests are left intact.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.orchestrator import events
import services.orchestrator.graph as _graph_mod
from services.orchestrator.types import Status, create_goal, update_status
from services.orchestrator.coding_orchestrator import (
    CodingOrchestrator,
    AsyncOrchestrator,
    Result,
)


@pytest.fixture(autouse=True)
def _enable_verify_gate(monkeypatch):
    """The verify-gate auto-critique is OFF by default in production
    (CRITIQUE_ARTIFACT_TYPES=""). These tests exercise that gate's bounding logic with a
    'code' artifact, so enable it for the duration of each test (auto-restored)."""
    monkeypatch.setattr(_graph_mod, "CRITIQUE_ARTIFACT_TYPES", ("code", "writing"))


@pytest.fixture
def fake_event_emitter(monkeypatch):
    """Capture events by monkeypatching events.emit (the nodes call the module fn)."""
    captured: list[tuple[str, dict]] = []

    async def fake_emit(type, **fields):
        captured.append((type, fields))

    monkeypatch.setattr(events, "emit", fake_emit)
    return captured


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _base_state(**overrides):
    tree = {}
    create_goal(tree, "root", None, "top-level task")
    state = {
        "session_id": "fix9",
        "goal_tree": tree,
        "current_goal_id": "root",
        "step_markers": {},
        "messages": [],
        "error": None,
        "root_goal": "top-level task",
        "last_artifact": {"type": "code", "payload": "def f(): return 1"},
        "verified": False,
        "critique_score": 1.0,
        "critique_notes": "",
        "verify_retries": 0,
    }
    state.update(overrides)
    return state


def _verify_node_with_score(score: float):
    """Build a verify node whose critique skill always returns `score`."""
    from services.orchestrator.graph import make_nodes

    mock_orch = MagicMock(spec=CodingOrchestrator)
    router_obj = MagicMock()
    router_obj.execute = AsyncMock(
        return_value={"ok": True, "result": {"score": score, "notes": "n"}}
    )
    mock_orch.skill_router = router_obj
    mock_async_orch = MagicMock(spec=AsyncOrchestrator)
    _, _, _, _, _, _, verify_node, _ = make_nodes(mock_orch, mock_async_orch)
    return verify_node


# ---------------------------------------------------------------------------
# (a) verify gate caps at MAX_VERIFY_RETRIES
# ---------------------------------------------------------------------------
@pytest.mark.mocked
@pytest.mark.asyncio
async def test_verify_gate_reflects_exactly_max_verify_retries_then_accepts(
    fake_event_emitter,
):
    """A persistently-low critique_score must reflect exactly MAX_VERIFY_RETRIES times,
    then route to check (accept). With the default MAX_VERIFY_RETRIES=1: first verify
    (prior=0) -> reflect; second verify (prior=1) -> check."""
    import services.orchestrator.graph as g

    assert g.MAX_VERIFY_RETRIES == 1  # default; this test reasons about the default

    verify_node = _verify_node_with_score(0.10)  # always below CRITIQUE_THRESHOLD

    # Pass 1: no prior retries -> should reflect, and commit verify_retries = 1.
    state = _base_state(verify_retries=0)
    d1 = await verify_node(state)
    assert d1["_verify_reflect"] is True
    assert d1["verify_retries"] == 1
    assert g.verify_router({**state, **d1}) == "reflect"

    # Pass 2: prior retries == 1 (== cap) -> must NOT reflect; accept and go to check.
    state2 = _base_state(verify_retries=d1["verify_retries"])
    d2 = await verify_node(state2)
    assert d2["_verify_reflect"] is False
    # No further increment once we accept.
    assert "verify_retries" not in d2 or d2.get("verify_retries") == 1
    assert g.verify_router({**state2, **d2}) == "check"


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_verify_gate_never_reflects_when_max_verify_retries_zero(
    monkeypatch, fake_event_emitter,
):
    """MAX_VERIFY_RETRIES=0 -> verify is advisory and never loops, even on a low score."""
    import services.orchestrator.graph as g

    monkeypatch.setattr(g, "MAX_VERIFY_RETRIES", 0)

    verify_node = _verify_node_with_score(0.10)
    state = _base_state(verify_retries=0)
    delta = await verify_node(state)
    assert delta["_verify_reflect"] is False
    assert "verify_retries" not in delta  # never incremented
    assert g.verify_router({**state, **delta}) == "check"


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_verify_gate_above_threshold_routes_to_check(fake_event_emitter):
    """An at/above-threshold artifact must never reflect regardless of retries."""
    import services.orchestrator.graph as g

    verify_node = _verify_node_with_score(0.95)
    state = _base_state(verify_retries=0)
    delta = await verify_node(state)
    assert delta["_verify_reflect"] is False
    assert g.verify_router({**state, **delta}) == "check"


# ---------------------------------------------------------------------------
# (b) classify_error + is_terminal (replaces _is_nonretryable_error)
# ---------------------------------------------------------------------------
@pytest.mark.mocked
@pytest.mark.parametrize(
    "err",
    [
        "SkillUnavailable: code-sandbox",
        "Docker is not running",
        "error: connection refused",
        "network unreachable",
        "Missing API key for Semantic Scholar",
        "permission denied (EPERM)",
        "no such file or directory (ENOENT)",
        "skill not found in registry",
    ],
)
def test_classify_error_marks_environmental_as_terminal(err):
    from services.orchestrator.error_classifier import classify_error, is_terminal

    cls = classify_error(err)
    assert is_terminal(cls) is True


@pytest.mark.mocked
@pytest.mark.parametrize(
    "err",
    [
        "request timed out after 30s",
        "Request timed out after 60s",
    ],
)
def test_classify_error_marks_timeout_as_transient(err):
    from services.orchestrator.error_classifier import classify_error, is_terminal
    from services.orchestrator.error_classifier import ErrorClass

    cls = classify_error(err)
    assert cls == ErrorClass.TRANSIENT
    assert is_terminal(cls) is False


@pytest.mark.mocked
@pytest.mark.parametrize(
    "err",
    [
        "Rate limit exceeded (429)",
        "HTTP 429 Too Many Requests",
    ],
)
def test_classify_error_marks_rate_limit_as_rate_limited(err):
    from services.orchestrator.error_classifier import classify_error
    from services.orchestrator.error_classifier import ErrorClass

    cls = classify_error(err)
    assert cls == ErrorClass.RATE_LIMITED


@pytest.mark.mocked
@pytest.mark.parametrize(
    "err",
    [
        "AssertionError: expected 5 got 4",
        "test_reverse failed: output mismatch",
        "ValueError: invalid literal for int()",
        "syntax error on line 12",
        "",
        None,
    ],
)
def test_classify_error_marks_unknown_as_retryable(err):
    from services.orchestrator.error_classifier import classify_error, is_terminal
    from services.orchestrator.error_classifier import ErrorClass

    cls = classify_error(err or "")
    assert cls == ErrorClass.RETRYABLE
    assert is_terminal(cls) is False


# ---------------------------------------------------------------------------
# (c) execute_node marks a non-retryable FAILED goal as exhausted
# ---------------------------------------------------------------------------
@pytest.mark.mocked
@pytest.mark.asyncio
async def test_execute_node_marks_nonretryable_failure_exhausted_and_check_finalizes(
    fake_event_emitter,
):
    """A non-retryable FAILED goal jumps straight to attempts == MAX_GOAL_ATTEMPTS, so
    check() finalizes with the error and the router does NOT route to reflect."""
    import services.orchestrator.graph as g
    from services.orchestrator.graph import make_nodes, router

    mock_orch = MagicMock(spec=CodingOrchestrator)
    mock_orch.skill_router = None
    mock_async_orch = MagicMock(spec=AsyncOrchestrator)

    state = _base_state()
    create_goal(state["goal_tree"], "child", "root", "needs docker")
    mock_async_orch.plan_and_dispatch = AsyncMock(
        return_value=[Result(id="child", summary="SkillUnavailable: Docker not running", ok=False)]
    )

    _, execute_node, check_node, _, _, _, _, _ = make_nodes(mock_orch, mock_async_orch)

    d_exec = await execute_node(state)
    # Marked exhausted in ONE pass (not incremented by 1).
    assert d_exec["goal_tree"]["child"]["attempts"] == g.MAX_GOAL_ATTEMPTS

    # check() must finalize (no retryable failures) and the router must NOT reflect.
    state_after = {**state, **d_exec}
    d_check = await check_node(state_after)
    assert "final_answer" in d_check
    assert "error" in d_check
    state_final = {**state_after, **d_check}
    # finalized -> revise (the gate), never "reflect"
    assert router(state_final) == "revise"


# ---------------------------------------------------------------------------
# (d) a retryable FAILED goal increments by 1 and is retried while < MAX_GOAL_ATTEMPTS
# ---------------------------------------------------------------------------
@pytest.mark.mocked
@pytest.mark.asyncio
async def test_execute_node_retryable_failure_increments_by_one_and_reflects(
    fake_event_emitter,
):
    import services.orchestrator.graph as g
    from services.orchestrator.graph import make_nodes, router  # noqa: F401

    mock_orch = MagicMock(spec=CodingOrchestrator)
    mock_orch.skill_router = None
    mock_async_orch = MagicMock(spec=AsyncOrchestrator)

    state = _base_state()
    create_goal(state["goal_tree"], "child", "root", "ordinary failing task")
    mock_async_orch.plan_and_dispatch = AsyncMock(
        return_value=[Result(id="child", summary="AssertionError: output mismatch", ok=False)]
    )

    _, execute_node, check_node, _, _, _, _, _ = make_nodes(mock_orch, mock_async_orch)

    d_exec = await execute_node(state)
    # Retryable -> increment by exactly 1 (from 0 -> 1), still < MAX_GOAL_ATTEMPTS (2).
    assert d_exec["goal_tree"]["child"]["attempts"] == 1
    assert d_exec["goal_tree"]["child"]["attempts"] < g.MAX_GOAL_ATTEMPTS

    # check() should defer (not finalize) and route to the failed child for reflect.
    state_after = {**state, **d_exec}
    d_check = await check_node(state_after)
    assert "final_answer" not in d_check
    assert d_check.get("current_goal_id") == "child"


# ---------------------------------------------------------------------------
# (e) reflect uses REFLECT_THINKING_BUDGET
# ---------------------------------------------------------------------------
@pytest.mark.mocked
@pytest.mark.asyncio
async def test_reflect_uses_reflect_thinking_budget(fake_event_emitter):
    import services.orchestrator.graph as g
    from services.orchestrator.graph import make_nodes

    mock_orch = MagicMock(spec=CodingOrchestrator)
    mock_orch.architect = AsyncMock(return_value="diagnosis")
    mock_async_orch = MagicMock(spec=AsyncOrchestrator)

    _, _, _, reflect_node, _, _, _, _ = make_nodes(mock_orch, mock_async_orch)

    state = _base_state(current_goal_id="child")
    create_goal(state["goal_tree"], "child", "root", "failing task")
    update_status(state["goal_tree"], "child", Status.FAILED, error="boom")
    state["goal_tree"]["child"]["attempts"] = 1

    await reflect_node(state)

    mock_orch.architect.assert_awaited_once()
    _, kwargs = mock_orch.architect.call_args
    assert kwargs.get("thinking_budget") == g.REFLECT_THINKING_BUDGET
