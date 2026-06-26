"""
FIX 9 regression tests (NEW file): verify-gate / retry-loop bounding.

These tests prove the Fix 9 invariants WITHOUT a GPU or live services:
  1. The verify<->reflect loop terminates (bounded by MAX_VERIFY_RETRIES).
  2. A deterministic/environmental ("non-retryable") failure bails immediately
     instead of paying MAX_GOAL_ATTEMPTS reflect+re-execute passes.
  3. An ordinary (retryable) failure still gets its full MAX_GOAL_ATTEMPTS passes.
  4. classify_error + is_terminal classifies error strings conservatively.
  5. verify_router is a pure function of the committed _verify_reflect flag and
     keeps the backward-compat threshold fallback.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.orchestrator.graph import (
    make_nodes,
    build_graph,
    verify_router,
    router,
    MAX_VERIFY_RETRIES,
    MAX_GOAL_ATTEMPTS,
    CRITIQUE_THRESHOLD,
)
from services.orchestrator.error_classifier import classify_error, is_terminal
from services.orchestrator.coding_orchestrator import (
    CodingOrchestrator,
    AsyncOrchestrator,
    Result,
)
from services.orchestrator.types import Status, create_goal
from langgraph.checkpoint.memory import MemorySaver
import services.orchestrator.graph as _graph_mod


@pytest.fixture(autouse=True)
def _enable_verify_gate(monkeypatch):
    """These tests exercise the verify->reflect gate mechanism with a 'code' artifact.
    The auto-gate is OFF by default in production (CRITIQUE_ARTIFACT_TYPES=""), so
    explicitly enable it here for the duration of each test (auto-restored)."""
    monkeypatch.setattr(_graph_mod, "CRITIQUE_ARTIFACT_TYPES", ("code", "writing"))


# --------------------------------------------------------------------------- #
# 4. classify_error + is_terminal (replaces _is_nonretryable_error)
# --------------------------------------------------------------------------- #
class TestClassifyError:
    @pytest.mark.parametrize("err", [
        "SkillUnavailable: code-sandbox",
        "docker daemon not running",
        "Connection refused",
        "network unreachable",
        "missing API key",
        "ENOENT: no such file",
        "permission denied (EPERM)",
    ])
    def test_marks_environmental_failures_as_terminal(self, err):
        cls = classify_error(err)
        assert is_terminal(cls) is True

    @pytest.mark.parametrize("err", [
        "request timed out",
        "Request timed out after 60s",
    ])
    def test_marks_timeout_as_transient(self, err):
        from services.orchestrator.error_classifier import ErrorClass
        cls = classify_error(err)
        assert cls == ErrorClass.TRANSIENT
        assert is_terminal(cls) is False

    @pytest.mark.parametrize("err", [
        "rate limit exceeded (429)",
        "HTTP 429 Too Many Requests",
    ])
    def test_marks_rate_limit_as_rate_limited(self, err):
        from services.orchestrator.error_classifier import ErrorClass
        cls = classify_error(err)
        assert cls == ErrorClass.RATE_LIMITED

    @pytest.mark.parametrize("err", [
        "AssertionError: expected 4 got 5",
        "ZeroDivisionError",
        "the function returned the wrong value",
        "syntax error on line 3",
        "",
    ])
    def test_marks_ordinary_logic_failures_as_retryable(self, err):
        from services.orchestrator.error_classifier import ErrorClass
        # These are exactly the failures a reflect-retry CAN fix; must stay retryable.
        cls = classify_error(err)
        assert cls == ErrorClass.RETRYABLE
        assert is_terminal(cls) is False


# --------------------------------------------------------------------------- #
# 5. verify_router purity + backward compat
# --------------------------------------------------------------------------- #
class TestVerifyRouter:
    def test_uses_committed_flag_when_present_true(self):
        # Even with a passing score, an explicit True flag routes to reflect.
        assert verify_router({"_verify_reflect": True, "critique_score": 1.0}) == "reflect"

    def test_uses_committed_flag_when_present_false(self):
        # Even with a failing score, an explicit False flag routes to check (bounded).
        assert verify_router({"_verify_reflect": False, "critique_score": 0.1}) == "check"

    def test_backward_compat_threshold_fallback_low(self):
        # No flag (hand-built old state): fall back to threshold comparison.
        assert verify_router({"critique_score": CRITIQUE_THRESHOLD - 0.5}) == "reflect"

    def test_backward_compat_threshold_fallback_high(self):
        assert verify_router({"critique_score": 1.0}) == "check"


# --------------------------------------------------------------------------- #
# 1. verify<->reflect loop is bounded (verify node logic)
# --------------------------------------------------------------------------- #
class TestVerifyLoopBounding:
    def _make_verify_node(self, score):
        """Build a verify node whose critique skill always returns `score`."""
        orch = MagicMock(spec=CodingOrchestrator)
        router_obj = MagicMock()
        router_obj.execute = AsyncMock(
            return_value={"ok": True, "result": {"score": score, "notes": "n"}}
        )
        orch.skill_router = router_obj
        aorch = MagicMock(spec=AsyncOrchestrator)
        nodes = make_nodes(orch, aorch)
        verify = nodes[6]
        assert verify.__name__ == "verify"
        return verify

    @pytest.mark.asyncio
    async def test_low_score_reflects_until_budget_then_accepts(self):
        """A persistently-low critique score must STOP reflecting after
        MAX_VERIFY_RETRIES passes. Drive the verify node directly, feeding back the
        committed verify_retries each pass, and assert it eventually routes to check."""
        verify = self._make_verify_node(score=0.10)  # always below threshold
        state = {
            "last_artifact": {"type": "code", "payload": "def f(): pass"},
            "root_goal": "x",
            "verify_retries": 0,
        }

        reflect_count = 0
        # Simulate the runtime loop: verify -> verify_router -> (reflect -> verify) ...
        # Hard cap iterations far above the budget to PROVE termination, not assume it.
        for _ in range(MAX_VERIFY_RETRIES + 10):
            out = await verify(state)
            decision = verify_router(out)
            # Persist committed deltas back into state, as LangGraph would merge them.
            state.update(out)
            if decision == "check":
                break
            reflect_count += 1
        else:
            pytest.fail("verify<->reflect loop did not terminate within bound")

        assert reflect_count == MAX_VERIFY_RETRIES, (
            f"expected exactly {MAX_VERIFY_RETRIES} reflect pass(es), got {reflect_count}"
        )
        assert decision == "check"
        assert state.get("verify_retries") == MAX_VERIFY_RETRIES

    @pytest.mark.asyncio
    async def test_passing_score_never_reflects(self):
        verify = self._make_verify_node(score=0.99)
        out = await verify({
            "last_artifact": {"type": "code", "payload": "def f(): pass"},
            "root_goal": "x",
            "verify_retries": 0,
        })
        assert out["_verify_reflect"] is False
        assert verify_router(out) == "check"
        assert "verify_retries" not in out  # not bumped when not reflecting

    @pytest.mark.asyncio
    async def test_non_code_artifact_never_reflects(self):
        verify = self._make_verify_node(score=0.10)
        out = await verify({
            "last_artifact": {"type": "other", "payload": "hi"},
            "root_goal": "x",
            "verify_retries": 0,
        })
        assert out["_verify_reflect"] is False
        assert verify_router(out) == "check"


# --------------------------------------------------------------------------- #
# 2 & 3. non-retryable bail vs. ordinary retry, exercised through execute_node
# --------------------------------------------------------------------------- #
class TestExecuteAttemptsBail:
    def _make_execute(self, summary, ok=False):
        orch = MagicMock(spec=CodingOrchestrator)
        orch.skill_router = None
        aorch = MagicMock(spec=AsyncOrchestrator)
        aorch.plan_and_dispatch = AsyncMock(
            return_value=[Result(id="root", summary=summary, ok=ok)]
        )
        nodes = make_nodes(orch, aorch)
        execute_node = nodes[1]
        assert execute_node.__name__ == "execute_node"
        return execute_node

    def _root_state(self):
        st = {
            "session_id": "s", "goal_tree": {}, "current_goal_id": "root",
            "step_markers": {}, "messages": [], "error": None, "final_answer": "",
            "workspace_id": "w", "user_id": "u",
        }
        create_goal(st["goal_tree"], "root", None, "do a thing")
        return st

    @pytest.mark.asyncio
    async def test_nonretryable_failure_jumps_attempts_to_cap(self):
        """A deterministic/environmental failure must be marked EXHAUSTED on the
        FIRST execute (attempts == MAX_GOAL_ATTEMPTS), so router won't reflect-retry."""
        execute_node = self._make_execute("SkillUnavailable: docker not available")
        st = self._root_state()
        out = await execute_node(st)
        tree = out["goal_tree"]
        assert tree["root"]["attempts"] == MAX_GOAL_ATTEMPTS
        # router must NOT reflect (would retry an identical guaranteed failure)
        st.update(out)
        assert router(st) != "reflect"

    @pytest.mark.asyncio
    async def test_ordinary_failure_increments_by_one(self):
        """An ordinary logic failure stays retryable: attempts increments by 1
        and router routes to reflect (legitimate retry preserved)."""
        execute_node = self._make_execute("AssertionError: wrong answer")
        st = self._root_state()
        out = await execute_node(st)
        tree = out["goal_tree"]
        assert tree["root"]["attempts"] == 1
        st.update(out)
        # With attempts=1 < MAX_GOAL_ATTEMPTS(>=2 by default), reflect is allowed.
        if MAX_GOAL_ATTEMPTS > 1:
            assert router(st) == "reflect"

    @pytest.mark.asyncio
    async def test_success_does_not_touch_attempts(self):
        execute_node = self._make_execute("def f(): return 1", ok=True)
        st = self._root_state()
        out = await execute_node(st)
        assert out["goal_tree"]["root"].get("attempts", 0) == 0


# --------------------------------------------------------------------------- #
# Full-graph termination: a non-retryable failure ends the task WITHOUT
# infinite looping, surfacing the error.
# --------------------------------------------------------------------------- #
class TestFullGraphNonretryableTermination:
    @pytest.mark.asyncio
    async def test_nonretryable_failure_terminates_graph(self):
        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.architect = AsyncMock(return_value="diag")
        mock_orch.skill_router = None
        mock_async = MagicMock(spec=AsyncOrchestrator)
        mock_async.plan_and_dispatch = AsyncMock(
            return_value=[Result(id="root_sub0",
                                 summary="ENOENT: docker socket missing", ok=False)]
        )
        real_cp = MemorySaver()
        with patch("pymongo.MongoClient"):
            with patch("langgraph.checkpoint.mongodb.MongoDBSaver", return_value=real_cp):
                graph, _ = build_graph(mock_orch, mock_async)
                st = {
                    "session_id": "nr", "goal_tree": {}, "current_goal_id": "root",
                    "step_markers": {}, "messages": [], "error": None,
                    "final_answer": "", "workspace_id": "w", "user_id": "u",
                    "verify_retries": 0,
                }
                create_goal(st["goal_tree"], "root", None, "Impossible env task")
                final = await graph.ainvoke(st, {"configurable": {"thread_id": "nr"}})

        assert final["goal_tree"]["root"]["status"] == Status.FAILED.value
        assert final.get("final_answer")
        # The bail must prevent any re-execution: exactly ONE dispatch (the first,
        # failing pass). A reflect-retry would dispatch the same goal again. Note we
        # cannot assert on architect.await_count because architect is ALSO called by
        # assess_ambiguity (triage) and plan (decompose) — only plan_and_dispatch
        # is a clean per-execute signal.
        assert mock_async.plan_and_dispatch.await_count == 1, (
            "non-retryable failure was re-dispatched — bail did not prevent retry"
        )
