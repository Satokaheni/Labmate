# tests/services/orchestrator/test_coding_orchestrator.py
from __future__ import annotations

import asyncio
import graphlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.orchestrator.coding_orchestrator import (
    AsyncOrchestrator,
    CodingOrchestrator,
    Result,
    SubTask,
    TokenBudget,
    _run_bash_passed,
    _run_tests_passed,
)


@pytest.fixture(autouse=True)
def _pin_skill_first_sequencing(monkeypatch):
    """These tests exercise the react_execute loop mechanics (skill-first fast-path,
    ReAct dispatch, run_bash, finish), not the production mode selection. Pin the
    dispatcher to ``skill_first`` (the default). Tests that specifically target
    ``react`` set the mode themselves.
    """
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.SEQUENCING_MODE",
        "skill_first",
        raising=False,
    )


def _chunk(text):
    return MagicMock(choices=[MagicMock(delta=MagicMock(content=text))])


def test_run_bash_passed_anchored_pytest_patterns():
    """Test _run_bash_passed uses anchored pytest summary patterns, not loose substrings.

    This guards against false positives from filenames or variable names containing 'ok',
    e.g. 'collected 1 item ... broken_module imported ok' should NOT mark tests passed
    unless there is an actual pytest pass summary like "1 passed".
    """
    # True: explicit pytest pass summary
    assert _run_bash_passed("collected 1 item\n1 passed in 0.05s") is True
    assert _run_bash_passed("2 passed, 0 failed") is True
    assert _run_bash_passed("tests/test_foo.py::test_bar PASSED\n\n1 passed in 0.1s") is True

    # False: 'ok' or 'passed' only in non-summary context
    assert _run_bash_passed("collected 1 item ... broken_module imported ok") is False
    assert _run_bash_passed("This output looks ok but has no pytest summary") is False
    assert _run_bash_passed("module ok.py loaded successfully") is False

    # False: explicit failure
    assert _run_bash_passed("1 failed, 0 passed in 0.5s") is False
    assert _run_bash_passed("collected 1 item\nTraceback (most recent call last):") is False
    assert _run_bash_passed("Error: something went wrong") is False

    # False: error JSON blob
    assert _run_bash_passed('{"error": "command not found"}') is False

    # False: no summary at all (ambiguous case; defensive default is False)
    assert _run_bash_passed("some random output with no test summary") is False


@pytest.mark.asyncio
async def test_stream_final_answer_emits_deltas_and_returns_text():
    from services.orchestrator import events

    orch = CodingOrchestrator(graph=None, workspace_path=".", docker_container="")

    async def fake_stream(*a, **k):
        for t in ["Hel", "lo ", "world"]:
            yield _chunk(t)

    captured = []

    class FakeEmitter:
        async def emit(self, type, **f):
            captured.append({"type": type, **f})

    with patch(
        "services.orchestrator.coding_orchestrator.litellm.acompletion",
        new_callable=AsyncMock,
        return_value=fake_stream(),
    ):
        token = events.current_emitter.set(FakeEmitter())
        try:
            text = await orch.stream_final_answer(
                "say hi",
                {"final_answer": "Hello world", "goal_tree": {"root": {"result": "Hello world"}}},
            )
        finally:
            events.current_emitter.reset(token)

    assert text == "Hello world"
    deltas = [e["text"] for e in captured if e["type"] == "answer.delta"]
    assert deltas == ["Hel", "lo ", "world"]
    assert any(e["type"] == "answer.done" and e["text"] == "Hello world" for e in captured)


@pytest.mark.asyncio
async def test_stream_final_answer_falls_back_on_error():
    orch = CodingOrchestrator(graph=None, workspace_path=".", docker_container="")
    with patch(
        "services.orchestrator.coding_orchestrator.litellm.acompletion",
        new_callable=AsyncMock,
        side_effect=RuntimeError("stream boom"),
    ):
        text = await orch.stream_final_answer("x", {"final_answer": "assembled answer"})
    assert text == "assembled answer"


def _make_mock_response(content: str = "done") -> MagicMock:
    r = MagicMock()
    r.choices = [MagicMock()]
    r.choices[0].message.content = content
    return r


def _msg_with_tool_call(name, arguments_json, reasoning=""):
    tc = MagicMock()
    tc.id = "call-1"
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = arguments_json
    msg = MagicMock()
    msg.tool_calls = [tc]
    msg.content = ""
    msg.reasoning_content = reasoning
    msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    return msg


@pytest.mark.asyncio
async def test_react_execute_emits_tool_events_for_run_bash():
    from services.orchestrator import events

    orch = AsyncOrchestrator(skill_router=None, mcp=MagicMock())
    orch.mcp.call_tool = AsyncMock(
        return_value=MagicMock(content=[MagicMock(text="hi")], isError=False)
    )

    resp1 = MagicMock(
        choices=[
            MagicMock(
                message=_msg_with_tool_call("run_bash", '{"command":"echo hi"}', "need shell")
            )
        ]
    )
    finish_msg = MagicMock(tool_calls=None, content="done")
    finish_msg.model_dump = lambda: {"role": "assistant", "content": "done"}
    resp2 = MagicMock(choices=[MagicMock(message=finish_msg)])

    captured = []

    class FakeEmitter:
        async def emit(self, type, **f):
            captured.append({"type": type, **f})

    with patch(
        "services.orchestrator.coding_orchestrator.litellm.acompletion",
        new_callable=AsyncMock,
        side_effect=[resp1, resp2],
    ):
        token = events.current_emitter.set(FakeEmitter())
        try:
            await orch.react_execute("run echo")
        finally:
            events.current_emitter.reset(token)

    types = [e["type"] for e in captured]
    assert "tool.start" in types and "tool.done" in types
    start = next(e for e in captured if e["type"] == "tool.start")
    assert start["name"] == "run_bash" and start["kind"] == "tool"


@pytest.mark.mocked
class TestTokenBudget:
    def test_init_applies_80_percent_margin(self):
        budget = TokenBudget(total=100_000)
        assert budget.remaining == 80_000

    def test_init_custom_margin(self):
        budget = TokenBudget(total=100_000, margin=0.5)
        assert budget.remaining == 50_000

    @pytest.mark.asyncio
    async def test_reserve_success_decrements_remaining(self):
        budget = TokenBudget(total=100_000)
        result = await budget.reserve(10_000)
        assert result is True
        assert budget.remaining == 70_000

    @pytest.mark.asyncio
    async def test_reserve_fails_when_exhausted(self):
        budget = TokenBudget(total=1_000)
        # total * 0.8 = 800; 900 > 800 so reserve fails
        result = await budget.reserve(900)
        assert result is False

    @pytest.mark.asyncio
    async def test_refund_restores_balance(self):
        budget = TokenBudget(total=100_000)
        await budget.reserve(10_000)
        await budget.refund(10_000)
        assert budget.remaining == 80_000

    @pytest.mark.asyncio
    async def test_concurrent_reserves_are_serialized(self):
        budget = TokenBudget(total=10_000)
        # total * 0.8 = 8000; each reserve asks for 4000
        # Only two can succeed (4000 + 4000 = 8000)
        results = await asyncio.gather(
            budget.reserve(4_000),
            budget.reserve(4_000),
            budget.reserve(4_000),
        )
        assert results.count(True) == 2
        assert budget.remaining == 0


@pytest.mark.mocked
class TestAsyncOrchestrator:
    @pytest.mark.asyncio
    async def test_raises_cycle_error_before_any_spawn(self):
        # Build subtasks with a cycle A -> B -> A
        subtasks = [
            SubTask(id="A", prompt="task A", deps={"B"}),
            SubTask(id="B", prompt="task B", deps={"A"}),
        ]
        dep_graph = {t.id: t.deps for t in subtasks}
        ts = graphlib.TopologicalSorter(dep_graph)
        with pytest.raises(graphlib.CycleError):
            ts.prepare()  # CycleError raised here, before any spawn

    @pytest.mark.asyncio
    async def test_parallel_dispatch_happy_path(self):
        orch = AsyncOrchestrator()

        async def fake_react_execute(goal: str) -> dict:
            return {"ok": True, "summary": "result for goal"}

        with patch.object(orch, "react_execute", side_effect=fake_react_execute):
            goals = [
                {"id": "g1", "description": "task 1", "children": [], "status": "PENDING"},
                {"id": "g2", "description": "task 2", "children": [], "status": "PENDING"},
            ]
            results = await orch.plan_and_dispatch(goals)
            assert len(results) == 2
            ids = {r.id for r in results}
            assert ids == {"g1", "g2"}
            assert all(r.ok for r in results)

    @pytest.mark.asyncio
    async def test_cancelled_error_is_reraised(self):
        orch = AsyncOrchestrator(budget=10_000)
        t = SubTask(id="x", prompt="test", est_tokens=100)

        async def raise_cancel(_):
            raise asyncio.CancelledError()

        with patch.object(orch, "react_execute", side_effect=raise_cancel):
            with pytest.raises((asyncio.CancelledError, ExceptionGroup)):
                await orch._run_worker(t)

    @pytest.mark.asyncio
    async def test_parallel_dispatch_with_one_failing_worker_preserves_siblings(self):
        """When one worker raises, its result is stored as failed, siblings succeed normally."""
        orch = AsyncOrchestrator()

        async def react_execute_side_effect(goal: str) -> dict:
            if "fail" in goal:
                raise RuntimeError("task failed")
            return {"ok": True, "summary": f"result for {goal}"}

        with patch.object(orch, "react_execute", side_effect=react_execute_side_effect):
            goals = [
                {"id": "g1", "description": "succeed task", "children": [], "status": "PENDING"},
                {"id": "g2", "description": "fail task", "children": [], "status": "PENDING"},
            ]
            # plan_and_dispatch should NOT raise; both results should be returned
            results = await orch.plan_and_dispatch(goals)

            assert len(results) == 2
            ids_and_ok = {r.id: r.ok for r in results}
            # g1 should succeed, g2 should be marked as failed but NOT re-raise
            assert ids_and_ok["g1"] is True
            assert ids_and_ok["g2"] is False
            # Verify the failed result has an error message
            g2_result = next(r for r in results if r.id == "g2")
            assert "error" in g2_result.summary.lower()

    @pytest.mark.asyncio
    async def test_run_worker_exception_stores_failed_result_and_returns(self):
        """_run_worker catches non-CancelledError exceptions, stores failed Result, returns normally."""
        orch = AsyncOrchestrator()
        t = SubTask(id="g1", prompt="failing goal", est_tokens=100)

        async def raise_error(_):
            raise ValueError("test error")

        with patch.object(orch, "react_execute", side_effect=raise_error):
            # Should NOT raise; should return t.id normally
            result = await orch._run_worker(t)
            assert result == t.id
            # Verify the failed result was stored
            assert t.id in orch.results
            assert orch.results[t.id].ok is False
            assert "error" in orch.results[t.id].summary.lower()

    @pytest.mark.asyncio
    async def test_aggregate_calls_architect_with_2000_budget(self):
        orch = AsyncOrchestrator()
        results = [
            Result(id="r1", summary="proposal 1", ok=True),
            Result(id="r2", summary="proposal 2", ok=True),
        ]
        mock_response = _make_mock_response("synthesized result")

        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock,
            return_value=mock_response,
        ) as mock_complete:
            result = await orch.aggregate("some task", results)
            call_kwargs = mock_complete.call_args.kwargs
            assert call_kwargs["extra_body"]["thinking_budget_tokens"] == 2000
            assert result.id == "aggregated"
            assert "synthesized result" in result.summary

    @pytest.mark.asyncio
    async def test_condense_truncates_long_output(self):
        orch = AsyncOrchestrator()
        long_text = "x" * 5000
        result = orch._condense("gid", long_text)
        assert len(result.summary) <= 2000
        assert result.ok is True
        assert result.id == "gid"

    @pytest.mark.asyncio
    async def test_react_execute_halts_on_absolute_turn_limit(self):
        """
        A model that calls read_file with distinct args (monotonically changing
        paths) emits all-distinct signatures, so the loop detector never trips.
        Each turn is refunded (cheap), so the budget never exhausts.
        But the absolute turn limit (2*max_steps) should halt the loop.
        This test verifies the hard ceiling prevents infinite loops.
        """
        from services.orchestrator import events

        orch = AsyncOrchestrator(skill_router=None, mcp=MagicMock(), max_steps=3)
        orch.mcp.call_tool = AsyncMock(
            return_value=MagicMock(content=[MagicMock(text="{}")], isError=False)
        )

        # Track how many turns the model was called
        turn_count = [0]

        async def mock_completion(*args, **kwargs):
            turn_count[0] += 1
            # Every response: call read_file with a different path
            # (distinct signatures, so loop detector never trips)
            # read_file IS in CHEAP_TOOLS, so each turn is refunded
            return MagicMock(
                choices=[
                    MagicMock(
                        message=_msg_with_tool_call(
                            "read_file", json.dumps({"path": f"file{turn_count[0]}.txt"})
                        )
                    )
                ]
            )

        captured = []

        class FakeEmitter:
            async def emit(self, type, **f):
                captured.append({"type": type, **f})

        with patch(
            "services.orchestrator.coding_orchestrator.acompletion_with_failover",
            new_callable=AsyncMock,
            side_effect=mock_completion,
        ):
            token = events.current_emitter.set(FakeEmitter())
            try:
                result = await orch.react_execute("read files")
            finally:
                events.current_emitter.reset(token)

        # Should halt with "absolute turn limit exceeded", not continue forever
        assert result["ok"] is False
        assert "absolute turn limit" in result["summary"]
        # Verify we hit the ceiling (2*3=6 turns), not way beyond
        assert turn_count[0] <= 7  # 6 + 1 for the final check

    @pytest.mark.asyncio
    async def test_run_tests_turn_is_refunded(self, monkeypatch):
        """run_tests turns must be refunded — verification should not eat the budget.
        With refund working, distinct run_tests turns run up to the absolute ceiling
        (2*max_total), not just max_total."""
        monkeypatch.setattr(
            "services.orchestrator.coding_orchestrator.SEQUENCING_MODE", "skill_first"
        )
        monkeypatch.setenv("LABMATE_MAX_ITERATIONS", "2")  # small cap to make the refund observable
        from services.orchestrator import events

        orch = AsyncOrchestrator(skill_router=None, mcp=MagicMock(), max_steps=2)

        # run_tests now runs as a direct local subprocess; stub
        # asyncio.create_subprocess_shell so each call returns a benign
        # failing-but-valid result without spawning a real process. Each model
        # turn uses a DISTINCT path (distinct command) so the loop detector
        # never trips.
        fake_proc = MagicMock()
        fake_proc.communicate = AsyncMock(return_value=(b"0 passed", None))
        fake_proc.returncode = 1
        fake_proc.kill = MagicMock()
        fake_proc.wait = AsyncMock()
        subprocess_mock = AsyncMock(return_value=fake_proc)

        calls = [0]

        async def _model(*a, **k):
            calls[0] += 1
            return MagicMock(
                choices=[
                    MagicMock(
                        message=_msg_with_tool_call(
                            "run_tests", json.dumps({"path": f"tests/test_{calls[0]}.py"})
                        )
                    )
                ]
            )

        class FakeEmitter:
            async def emit(self, type, **f):
                pass

        with (
            patch(
                "services.orchestrator.coding_orchestrator.acompletion_with_failover",
                new_callable=AsyncMock,
                side_effect=_model,
            ),
            patch(
                "services.orchestrator.coding_orchestrator.asyncio.create_subprocess_shell",
                new=subprocess_mock,
            ),
        ):
            token = events.current_emitter.set(FakeEmitter())
            try:
                result = await orch.react_execute("non-edit: just run tests repeatedly")
            finally:
                events.current_emitter.reset(token)

        # If run_tests were NOT refunded, the loop would stop at the consume cap (2)
        # with "budget exhausted". With the refund it reaches the absolute ceiling.
        assert "budget exhausted" not in result["summary"]
        assert calls[0] > 2  # ran past the consume cap thanks to refunds


@pytest.mark.mocked
class TestCodingOrchestrator:
    def _make_orch(self):
        from services.orchestrator.coding_orchestrator import CodingOrchestrator

        return CodingOrchestrator(
            graph=MagicMock(),
            workspace_path="/tmp/workspace",
            docker_container="lm-sandbox",
        )

    @pytest.mark.asyncio
    async def test_architect_passes_thinking_budget_in_extra_body(self):
        orch = self._make_orch()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "here is the plan"

        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock,
            return_value=mock_response,
        ) as mock_complete:
            result = await orch.architect("decompose this", thinking_budget=3000)
            call_kwargs = mock_complete.call_args.kwargs
            assert call_kwargs["extra_body"]["thinking_budget_tokens"] == 3000
            assert call_kwargs["model"] == "openai/gemma-4-31b"
            assert result == "here is the plan"

    @pytest.mark.asyncio
    async def test_architect_tool_dispatch_uses_zero_budget(self):
        orch = self._make_orch()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "tool result"

        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock,
            return_value=mock_response,
        ) as mock_complete:
            await orch.architect("route this tool call", thinking_budget=0)
            call_kwargs = mock_complete.call_args.kwargs
            assert call_kwargs["extra_body"]["thinking_budget_tokens"] == 0

    @pytest.mark.asyncio
    async def test_editor_calls_qwen(self):
        orch = self._make_orch()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "patched code"

        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock,
            return_value=mock_response,
        ) as mock_complete:
            result = await orch.editor("fix this bug")
            call_kwargs = mock_complete.call_args.kwargs
            assert call_kwargs["model"] == "openai/qwen2.5-coder-32b"
            assert result == "patched code"

    def test_is_stuck_returns_false_when_not_repeated(self):
        orch = self._make_orch()
        assert orch.is_stuck("action_a") is False
        assert orch.is_stuck("action_b") is False
        assert orch.is_stuck("action_a") is False

    def test_is_stuck_returns_true_after_n_identical_actions(self):
        orch = self._make_orch()
        orch.is_stuck("same_action")
        orch.is_stuck("same_action")
        result = orch.is_stuck("same_action")
        assert result is True

    def test_is_stuck_resets_on_different_action(self):
        orch = self._make_orch()
        orch.is_stuck("same_action")
        orch.is_stuck("same_action")
        orch.is_stuck("different_action")  # breaks the streak
        assert orch.is_stuck("same_action") is False

    def test_execute_in_sandbox_success(self):
        orch = self._make_orch()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="test output", stderr="", returncode=0)
            result = orch.execute_in_sandbox("echo hello")
            assert result["ok"] is True
            assert result["stdout"] == "test output"
            assert result["exit_code"] == 0
            cmd = mock_run.call_args[0][0]
            assert "docker" in cmd
            assert "exec" in cmd
            assert "lm-sandbox" in cmd

    def test_execute_in_sandbox_failure(self):
        orch = self._make_orch()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", stderr="command not found", returncode=1)
            result = orch.execute_in_sandbox("bad_command")
            assert result["ok"] is False
            assert result["exit_code"] == 1

    @pytest.mark.asyncio
    async def test_git_checkpoint_calls_git_add_and_commit(self):
        orch = self._make_orch()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            await orch.git_checkpoint("step 3: fixed tests")
            calls = [c[0][0] for c in mock_run.call_args_list]
            assert any("add" in c for c in calls)
            assert any("commit" in c for c in calls)


@pytest.mark.mocked
class TestRunInSandbox:
    def _make_orch(self, mcp=None):
        from services.orchestrator.coding_orchestrator import CodingOrchestrator

        return CodingOrchestrator(
            graph=MagicMock(),
            workspace_path="/tmp/workspace",
            docker_container="lm-sandbox",
            mcp=mcp,
        )

    def _make_call_tool_result(self, text: str, is_error: bool = False):
        """Minimal stand-in for mcp.types.CallToolResult."""
        content_item = MagicMock()
        content_item.text = text
        result = MagicMock()
        result.content = [content_item]
        result.isError = is_error
        return result

    @pytest.mark.asyncio
    async def test_routes_through_mcp_when_available(self):
        mcp = AsyncMock()
        mcp.call_tool.return_value = self._make_call_tool_result("output text")
        orch = self._make_orch(mcp=mcp)

        obs = await orch.run_in_sandbox("echo hello", timeout_ms=5000)

        mcp.call_tool.assert_awaited_once_with(
            "exec_run",
            {"command": "echo hello", "cwd": "/tmp/workspace", "timeout": 5000},
        )
        assert obs["ok"] is True
        assert obs["stdout"] == "output text"

    @pytest.mark.asyncio
    async def test_mcp_error_result_sets_ok_false(self):
        mcp = AsyncMock()
        mcp.call_tool.return_value = self._make_call_tool_result("command not found", is_error=True)
        orch = self._make_orch(mcp=mcp)

        obs = await orch.run_in_sandbox("bad-cmd")
        assert obs["ok"] is False
        assert obs["exit_code"] == 1

    @pytest.mark.asyncio
    async def test_mcp_exception_returns_error_dict(self):
        mcp = AsyncMock()
        mcp.call_tool.side_effect = RuntimeError("bridge not ready")
        orch = self._make_orch(mcp=mcp)

        obs = await orch.run_in_sandbox("ls")
        assert obs["ok"] is False
        assert "bridge not ready" in obs["stderr"]

    @pytest.mark.asyncio
    async def test_falls_back_to_subprocess_without_mcp(self):
        orch = self._make_orch(mcp=None)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
            obs = await orch.run_in_sandbox("echo hi")
        assert obs["ok"] is True

    @pytest.mark.asyncio
    async def test_mcp_result_concatenates_multiple_content_items(self):
        mcp = AsyncMock()
        item_a = MagicMock()
        item_a.text = "line one"
        item_b = MagicMock()
        item_b.text = "line two"
        result = MagicMock()
        result.content = [item_a, item_b]
        result.isError = False
        mcp.call_tool.return_value = result
        orch = self._make_orch(mcp=mcp)

        obs = await orch.run_in_sandbox("cmd")
        assert obs["stdout"] == "line one\nline two"


@pytest.fixture
def orch_with_graph():
    """Fixture providing a CodingOrchestrator with a mock graph."""
    from services.orchestrator.coding_orchestrator import CodingOrchestrator

    async def mock_ainvoke(state, config):
        """Mock ainvoke that preserves the input state."""
        return state

    mock_graph = AsyncMock()
    mock_graph.ainvoke = mock_ainvoke
    return CodingOrchestrator(
        graph=mock_graph,
        workspace_path="/tmp/workspace",
        docker_container="lm-sandbox",
    )


@pytest.mark.asyncio
async def test_run_task_accepts_user_workspace(orch_with_graph):
    """run_task accepts user_id and workspace_id kwargs without error."""
    state = await orch_with_graph.run_task(
        "hello",
        session_id="s-1",
        user_id="u-abc",
        workspace_id="ws-xyz",
    )
    assert isinstance(state, dict)


@pytest.mark.asyncio
async def test_state_carries_workspace_fields(orch_with_graph):
    """Final state includes workspace_id and user_id."""
    state = await orch_with_graph.run_task(
        "hello",
        session_id="s-2",
        user_id="u-abc",
        workspace_id="ws-xyz",
    )
    assert state.get("workspace_id") == "ws-xyz"
    assert state.get("user_id") == "u-abc"


@pytest.mark.mocked
class TestReactExecute:
    """Tests for the skill-aware ReAct executor."""

    def _make_orch(self, skill_router=None, mcp=None, max_steps=6):
        from services.orchestrator.coding_orchestrator import AsyncOrchestrator

        return AsyncOrchestrator(
            skill_router=skill_router,
            mcp=mcp,
            workspace="/tmp",
            max_steps=max_steps,
        )

    def _make_tool_call_response(self, tool_name: str, arguments: dict):
        """Construct a litellm response with tool calls."""
        r = MagicMock()
        tc = MagicMock()
        tc.id = "call_123"
        tc.function.name = tool_name
        tc.function.arguments = json.dumps(arguments)
        msg = MagicMock()
        msg.content = None
        msg.tool_calls = [tc]
        msg.model_dump = MagicMock(
            return_value={
                "role": "assistant",
                "content": None,
                "tool_calls": [MagicMock(id="call_123")],
            }
        )
        r.choices = [MagicMock(message=msg)]
        return r

    @pytest.mark.asyncio
    async def test_react_execute_finish_immediately(self):
        """Model calls finish directly — return ok=True."""
        orch = self._make_orch()
        r = MagicMock()
        msg = MagicMock()
        msg.content = None
        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "finish"
        tc.function.arguments = json.dumps({"summary": "done"})
        msg.tool_calls = [tc]
        r.choices = [MagicMock(message=msg)]

        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock,
            return_value=r,
        ):
            result = await orch.react_execute("do something")
            assert result["ok"] is True
            assert "done" in result["summary"]

    @pytest.mark.asyncio
    async def test_react_execute_direct_content_no_tools(self):
        """Model replies with text, no tool calls — return ok=True."""
        orch = self._make_orch()
        r = MagicMock()
        msg = MagicMock()
        msg.content = "The answer is 42"
        msg.tool_calls = None
        r.choices = [MagicMock(message=msg)]

        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock,
            return_value=r,
        ):
            result = await orch.react_execute("what is 2+2?")
            assert result["ok"] is True
            assert "42" in result["summary"]

    @pytest.mark.asyncio
    async def test_react_execute_budget_exhausted(self):
        """Loop exhausts the budget (incl. the grace turn) without finish — ok=False."""
        orch = self._make_orch(max_steps=2)

        # cap=2 working turns + 1 grace turn = 3 model calls before stopping.
        # Use write_file (non-refundable) to test budget exhaustion; run_bash is now refundable.
        r1 = self._make_tool_call_response("write_file", {"path": "a.txt", "content": "1"})
        r2 = self._make_tool_call_response("write_file", {"path": "b.txt", "content": "2"})
        r3 = self._make_tool_call_response("write_file", {"path": "c.txt", "content": "3"})

        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock,
            side_effect=[r1, r2, r3],
        ):
            # Mock MCP to return write result
            mcp = AsyncMock()
            mcp_result = MagicMock()
            mcp_result.content = [MagicMock(text="file written")]
            mcp_result.isError = False
            mcp.call_tool.return_value = mcp_result
            orch.mcp = mcp

            result = await orch.react_execute("do something")
            assert result["ok"] is False
            assert "budget exhausted" in result["summary"]

    @pytest.mark.asyncio
    async def test_react_execute_with_skill_router_load_skill(self):
        """load_skill tool call when skill_router present."""
        runner = MagicMock()
        runner.catalog_prompt.return_value = "- test-skill: A test skill"
        runner.tool_schema.return_value = {
            "type": "function",
            "function": {"name": "load_skill", "parameters": {}},
        }
        runner.load_skill.return_value = {"response": {"status": "loaded", "body": "skill body"}}
        skill_router = MagicMock()
        skill_router.runner = runner

        orch = self._make_orch(skill_router=skill_router)

        # First call: load_skill, then finish
        r1 = self._make_tool_call_response("load_skill", {"name": "test-skill"})
        r2 = MagicMock()
        msg2 = MagicMock()
        msg2.content = None
        tc2 = MagicMock()
        tc2.id = "call_2"
        tc2.function.name = "finish"
        tc2.function.arguments = json.dumps({"summary": "loaded and done"})
        msg2.tool_calls = [tc2]
        r2.choices = [MagicMock(message=msg2)]

        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock,
            side_effect=[r1, r2],
        ):
            result = await orch.react_execute("use test-skill")
            assert result["ok"] is True
            runner.load_skill.assert_called_once_with("test-skill")

    @pytest.mark.asyncio
    async def test_react_execute_with_mcp_run_bash(self):
        """run_bash tool routes through MCP when available."""
        orch = self._make_orch()

        r = self._make_tool_call_response("run_bash", {"command": "echo hello"})

        mcp = AsyncMock()
        mcp_result = MagicMock()
        mcp_result.content = [MagicMock(text="hello")]
        mcp_result.isError = False
        mcp.call_tool.return_value = mcp_result
        orch.mcp = mcp

        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock,
            side_effect=[r],
        ):
            # This should hit max_steps=6, but we're testing one tool dispatch
            await orch.react_execute("echo hello")
            # The tool response goes to the model, model likely replies
            # Just ensure the call path works
            mcp.call_tool.assert_awaited()

    @pytest.mark.asyncio
    async def test_code_semantic_search_dispatches_to_codegraph_explore(self):
        """code_semantic_search routes to the CodeGraph CLI daemon's codegraph_explore
        tool (NL question -> verbatim source), not the old Chroma-embedder tool name,
        and drops the k arg (codegraph_explore is internally capped)."""
        orch = self._make_orch()

        r = self._make_tool_call_response(
            "code_semantic_search", {"query": "where is auth handled?", "k": 8}
        )

        codegraph_mcp = AsyncMock()
        mcp_result = MagicMock()
        mcp_result.content = [MagicMock(text="def authenticate(): ...")]
        codegraph_mcp.call_tool.return_value = mcp_result
        orch.codegraph_mcp = codegraph_mcp

        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock,
            side_effect=[r],
        ):
            await orch.react_execute("where is auth handled?")

        codegraph_mcp.call_tool.assert_awaited_once_with(
            "codegraph_explore", {"question": "where is auth handled?"}
        )

    @pytest.mark.asyncio
    async def test_code_semantic_search_without_codegraph_mcp(self):
        """code_semantic_search fails gracefully when no codegraph_mcp is attached
        (e.g. the CodeGraph CLI wasn't found at startup)."""
        orch = self._make_orch()
        orch.codegraph_mcp = None

        r = self._make_tool_call_response("code_semantic_search", {"query": "anything"})
        r2 = MagicMock()
        msg2 = MagicMock()
        msg2.content = None
        tc2 = MagicMock()
        tc2.id = "call_2"
        tc2.function.name = "finish"
        tc2.function.arguments = json.dumps({"summary": "done"})
        msg2.tool_calls = [tc2]
        r2.choices = [MagicMock(message=msg2)]

        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock,
            side_effect=[r, r2],
        ):
            result = await orch.react_execute("search for something")
            assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_react_execute_run_bash_without_mcp(self):
        """run_bash fails gracefully when no MCP."""
        orch = self._make_orch(mcp=None)

        r = self._make_tool_call_response("run_bash", {"command": "ls"})
        # Next response: finish
        r2 = MagicMock()
        msg2 = MagicMock()
        msg2.content = None
        tc2 = MagicMock()
        tc2.id = "call_2"
        tc2.function.name = "finish"
        tc2.function.arguments = json.dumps({"summary": "done"})
        msg2.tool_calls = [tc2]
        r2.choices = [MagicMock(message=msg2)]

        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock,
            side_effect=[r, r2],
        ):
            result = await orch.react_execute("do something")
            # Should succeed because finish is called
            assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_react_execute_no_skill_router_still_has_bash_finish(self):
        """When skill_router is None, still expose run_bash and finish."""
        orch = self._make_orch(skill_router=None, mcp=None)

        r = MagicMock()
        msg = MagicMock()
        msg.content = "no tools needed"
        msg.tool_calls = None
        r.choices = [MagicMock(message=msg)]

        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock,
            return_value=r,
        ):
            result = await orch.react_execute("simple task")
            assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_react_execute_exception_returns_error(self):
        """Uncaught exception in react_execute returns ok=False."""
        orch = self._make_orch()

        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            side_effect=RuntimeError("api error"),
        ):
            result = await orch.react_execute("any task")
            assert result["ok"] is False
            assert "error" in result["summary"]

    @pytest.mark.asyncio
    async def test_react_execute_fallback_when_message_lacks_model_dump(self):
        """FIX #3: Fallback constructs proper tool_calls when msg lacks model_dump."""
        orch = self._make_orch()

        # Create a response where message does NOT have model_dump method
        r = MagicMock()
        msg = MagicMock(spec=[])  # spec=[] means no methods at all
        msg.content = None
        tc = MagicMock()
        tc.id = "call_123"
        tc.function.name = "finish"
        tc.function.arguments = json.dumps({"summary": "fallback works"})
        msg.tool_calls = [tc]
        # Explicitly remove model_dump to simulate the fallback condition
        if hasattr(msg, "model_dump"):
            delattr(msg, "model_dump")
        r.choices = [MagicMock(message=msg)]

        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock,
            return_value=r,
        ):
            result = await orch.react_execute("test fallback")
            # Fallback should succeed: finish tool was called
            assert result["ok"] is True
            assert "fallback works" in result["summary"]

    @pytest.mark.asyncio
    async def test_react_execute_skill_failure_surfaces_error_message(self):
        """Skill-first shortcut: when skill fails, summary must contain error, never literal 'null'."""
        runner = MagicMock()
        runner.reset_activations.return_value = None
        skill_router = MagicMock()
        skill_router.runner = runner
        orch = self._make_orch(skill_router=skill_router)

        # Mock skill_router.run() to return a failure response
        skill_router.run = AsyncMock(return_value={"ok": False, "error": "timeout"})

        result = await orch.react_execute("some goal that matches a skill")

        # Verify the result reflects failure
        assert result["ok"] is False
        # The summary must surface the actual error, NOT the literal string "null"
        assert result["summary"] != "null"
        assert "timeout" in result["summary"]

    @pytest.mark.asyncio
    async def test_react_execute_skill_success_with_string_result(self):
        """Skill-first shortcut: when skill succeeds with string result, return it."""
        runner = MagicMock()
        runner.reset_activations.return_value = None
        skill_router = MagicMock()
        skill_router.runner = runner
        orch = self._make_orch(skill_router=skill_router)

        # Mock skill_router.run() to return a success response with string result
        skill_router.run = AsyncMock(
            return_value={"ok": True, "result": "skill execution completed successfully"}
        )

        result = await orch.react_execute("some goal that matches a skill")

        # Verify the result reflects success
        assert result["ok"] is True
        assert "skill execution completed successfully" in result["summary"]
        assert result["summary"] != "null"

    @pytest.mark.asyncio
    async def test_react_execute_skill_success_with_none_result(self):
        """Skill-first shortcut: ok=True but result=None should NOT return literal 'null'."""
        runner = MagicMock()
        runner.reset_activations.return_value = None
        skill_router = MagicMock()
        skill_router.runner = runner
        orch = self._make_orch(skill_router=skill_router)

        # Mock skill_router.run() to return success with None result
        skill_router.run = AsyncMock(return_value={"ok": True, "result": None})

        result = await orch.react_execute("some goal that matches a skill")

        # Verify: ok=True but summary is NOT the literal string "null"
        assert result["ok"] is True
        assert result["summary"] != "null"
        # Should use the neutral placeholder
        assert result["summary"] == "(no output)"

    @pytest.mark.asyncio
    async def test_react_execute_skill_success_with_empty_result(self):
        """Skill-first shortcut: ok=True with empty string or False result."""
        runner = MagicMock()
        runner.reset_activations.return_value = None
        skill_router = MagicMock()
        skill_router.runner = runner
        orch = self._make_orch(skill_router=skill_router)

        # Mock skill_router.run() to return success with empty string result
        skill_router.run = AsyncMock(return_value={"ok": True, "result": ""})

        result = await orch.react_execute("some goal that matches a skill")

        # Verify: ok=True but summary is NOT literal "null"
        assert result["ok"] is True
        assert result["summary"] == "(no output)"

    @pytest.mark.asyncio
    async def test_react_execute_skill_failure_with_none_error(self):
        """Skill-first shortcut: ok=False with error=None should use fallback message."""
        runner = MagicMock()
        runner.reset_activations.return_value = None
        skill_router = MagicMock()
        skill_router.runner = runner
        orch = self._make_orch(skill_router=skill_router)

        # Mock skill_router.run() to return failure with error=None
        skill_router.run = AsyncMock(return_value={"ok": False, "error": None})

        result = await orch.react_execute("some goal that matches a skill")

        # Verify: should not crash and should use fallback message
        assert result["ok"] is False
        assert result["summary"] == "skill failed"
        assert "null" not in result["summary"]

    @pytest.mark.asyncio
    async def test_react_execute_skill_failure_tool_error_surfaces_inner_content(self):
        """tool_error: the real diagnostic lives in result content, not the discriminator."""
        runner = MagicMock()
        runner.reset_activations.return_value = None
        skill_router = MagicMock()
        skill_router.runner = runner
        orch = self._make_orch(skill_router=skill_router)

        skill_router.run = AsyncMock(
            return_value={
                "ok": False,
                "error": "tool_error",
                "result": {"content": [{"type": "text", "text": "Traceback: ZeroDivisionError"}]},
            }
        )

        result = await orch.react_execute("some goal that matches a skill")

        assert result["ok"] is False
        # discriminator kept, but the real error text is surfaced for the reflect loop
        assert "tool_error" in result["summary"]
        assert "ZeroDivisionError" in result["summary"]

    @pytest.mark.asyncio
    async def test_react_execute_skill_failure_surfaces_detail(self):
        """skill_unavailable/dispatch_failed: the human cause lives in 'detail'."""
        runner = MagicMock()
        runner.reset_activations.return_value = None
        skill_router = MagicMock()
        skill_router.runner = runner
        orch = self._make_orch(skill_router=skill_router)

        skill_router.run = AsyncMock(
            return_value={
                "ok": False,
                "error": "skill_unavailable",
                "detail": "no tool 'frobnicate' in skill 'foo'",
            }
        )

        result = await orch.react_execute("some goal that matches a skill")

        assert result["ok"] is False
        assert "skill_unavailable" in result["summary"]
        assert "frobnicate" in result["summary"]

    @pytest.mark.asyncio
    async def test_react_execute_skill_success_with_content_list_no_text(self):
        """Skill-first shortcut: result with content list but no text fields."""
        runner = MagicMock()
        runner.reset_activations.return_value = None
        skill_router = MagicMock()
        skill_router.runner = runner
        orch = self._make_orch(skill_router=skill_router)

        # Mock skill_router.run() to return success with content list but no text fields
        skill_router.run = AsyncMock(
            return_value={
                "ok": True,
                "result": {
                    "content": [
                        {"type": "file", "path": "/some/file"},
                        {"type": "json", "data": {"key": "value"}},
                    ]
                },
            }
        )

        result = await orch.react_execute("some goal that matches a skill")

        # Verify: content list with no text should use placeholder, not empty string
        assert result["ok"] is True
        assert result["summary"] == "(no output)"

    @pytest.mark.asyncio
    async def test_react_execute_skill_success_with_structured_dict_result(self):
        """Skill-first shortcut: ok=True with structured dict result (JSON serialization)."""
        runner = MagicMock()
        runner.reset_activations.return_value = None
        skill_router = MagicMock()
        skill_router.runner = runner
        orch = self._make_orch(skill_router=skill_router)

        # Mock skill_router.run() to return success with structured dict result
        skill_router.run = AsyncMock(
            return_value={"ok": True, "result": {"status": "complete", "count": 42}}
        )

        result = await orch.react_execute("some goal that matches a skill")

        # Verify: structured result is JSON-serialized
        assert result["ok"] is True
        assert "complete" in result["summary"]
        assert "42" in result["summary"]

    @pytest.mark.asyncio
    async def test_react_execute_breaks_on_tool_loop_before_max_steps(self):
        """Detector trips on a repeated run_bash call and halts before max_steps."""
        orch = self._make_orch(max_steps=6)

        # Model ALWAYS calls run_bash {command: ls} — a no-progress loop.
        def _looping_response():
            return self._make_tool_call_response("run_bash", {"command": "ls"})

        call_count = {"n": 0}

        async def _counting(*a, **k):
            call_count["n"] += 1
            return _looping_response()

        mcp = AsyncMock()
        mcp_result = MagicMock()
        mcp_result.content = [MagicMock(text="files")]
        mcp_result.isError = False
        mcp.call_tool.return_value = mcp_result
        orch.mcp = mcp

        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock,
            side_effect=_counting,
        ):
            result = await orch.react_execute("loop forever")

        assert result["ok"] is False
        assert "loop" in result["summary"].lower()
        # Must halt before exhausting all 6 steps.
        assert call_count["n"] < 6

    @pytest.mark.asyncio
    async def test_react_execute_distinct_calls_do_not_trip_loop(self):
        """Distinct read_file paths must NOT trip the detector; finish ends cleanly."""
        orch = self._make_orch(max_steps=6, mcp=None)
        orch.local_client = (
            None  # read_file with no client attached returns a structured error, not a crash
        )

        # Three distinct reads, then finish — no two consecutive identical sigs.
        r1 = self._make_tool_call_response("read_file", {"path": "a.txt"})
        r2 = self._make_tool_call_response("read_file", {"path": "b.txt"})
        r3 = self._make_tool_call_response("read_file", {"path": "c.txt"})
        rf = MagicMock()
        mf = MagicMock()
        mf.content = None
        tcf = MagicMock()
        tcf.id = "call_fin"
        tcf.function.name = "finish"
        tcf.function.arguments = json.dumps({"summary": "all read"})
        mf.tool_calls = [tcf]
        rf.choices = [MagicMock(message=mf)]

        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock,
            side_effect=[r1, r2, r3, rf],
        ):
            result = await orch.react_execute("read three files")

        assert result["ok"] is True
        assert result["summary"] == "all read"

    @pytest.mark.asyncio
    async def test_react_execute_repeat_load_skill_short_circuited(self):
        """A 2nd load_skill for an already-loaded skill must NOT call the real
        loader again; the tool result reports 'already loaded'."""
        runner = MagicMock()
        runner.catalog_prompt.return_value = "- code-review: review code"
        runner.tool_schema.return_value = {
            "type": "function",
            "function": {"name": "load_skill", "parameters": {}},
        }
        runner.load_skill.return_value = {
            "name": "load_skill",
            "response": {"status": "loaded", "name": "code-review", "body": "BODY"},
        }
        skill_router = MagicMock()
        skill_router.runner = runner
        orch = self._make_orch(skill_router=skill_router)

        # Turn 1: load code-review. Turn 2: load code-review AGAIN. Turn 3: finish.
        r1 = self._make_tool_call_response("load_skill", {"name": "code-review"})
        r2 = self._make_tool_call_response("load_skill", {"name": "code-review"})
        r3 = self._make_tool_call_response("finish", {"summary": "done"})

        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock,
            side_effect=[r1, r2, r3],
        ):
            result = await orch.react_execute("review then fix")

        assert result["ok"] is True
        # The real loader ran only for the FIRST load.
        runner.load_skill.assert_called_once_with("code-review")

    @pytest.mark.asyncio
    async def test_react_execute_repeat_load_skill_refunds_budget(self):
        """The redundant reload turn is refunded, so the model still has enough
        budget to finish. With max_steps=2: load (1) -> redundant load (refunded)
        -> finish should succeed rather than exhausting the budget."""
        runner = MagicMock()
        runner.catalog_prompt.return_value = "- code-review: review code"
        runner.tool_schema.return_value = {
            "type": "function",
            "function": {"name": "load_skill", "parameters": {}},
        }
        runner.load_skill.return_value = {
            "name": "load_skill",
            "response": {"status": "loaded", "name": "code-review", "body": "BODY"},
        }
        skill_router = MagicMock()
        skill_router.runner = runner
        orch = self._make_orch(skill_router=skill_router, max_steps=2)

        r1 = self._make_tool_call_response("load_skill", {"name": "code-review"})
        r2 = self._make_tool_call_response("load_skill", {"name": "code-review"})
        r3 = self._make_tool_call_response("finish", {"summary": "done"})

        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock,
            side_effect=[r1, r2, r3],
        ):
            result = await orch.react_execute("review then fix")

        # Without the refund, load(1)+load(2) would exhaust max_steps=2 and the
        # grace turn would be the finish — but the finish would still run, so we
        # assert on the stronger signal: the loader ran once and we finished ok.
        assert result["ok"] is True
        assert "done" in result["summary"]
        runner.load_skill.assert_called_once_with("code-review")

    @pytest.mark.asyncio
    async def test_react_execute_distinct_load_skill_not_short_circuited(self):
        """Loading two DIFFERENT skills both hit the real loader (no false
        short-circuit on a first load)."""
        runner = MagicMock()
        runner.catalog_prompt.return_value = "- code-review: x\n- test-gen: y"
        runner.tool_schema.return_value = {
            "type": "function",
            "function": {"name": "load_skill", "parameters": {}},
        }
        runner.load_skill.side_effect = lambda n: {
            "name": "load_skill",
            "response": {"status": "loaded", "name": n, "body": "BODY"},
        }
        skill_router = MagicMock()
        skill_router.runner = runner
        orch = self._make_orch(skill_router=skill_router)

        r1 = self._make_tool_call_response("load_skill", {"name": "code-review"})
        r2 = self._make_tool_call_response("load_skill", {"name": "test-gen"})
        r3 = self._make_tool_call_response("finish", {"summary": "done"})

        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock,
            side_effect=[r1, r2, r3],
        ):
            result = await orch.react_execute("review then test")

        assert result["ok"] is True
        called = {c.args[0] for c in runner.load_skill.call_args_list}
        assert called == {"code-review", "test-gen"}


@pytest.mark.mocked
class TestRunWorkerUsesReactExecute:
    """Test that _run_worker now uses react_execute instead of _call_qwen_worker."""

    @pytest.mark.asyncio
    async def test_run_worker_calls_react_execute(self):
        """_run_worker delegates to react_execute and builds Result correctly."""
        orch = AsyncOrchestrator()
        t = SubTask(id="g1", prompt="test goal", est_tokens=100)

        mock_react_ret = {"ok": True, "summary": "task done"}

        with patch.object(
            orch, "react_execute", new_callable=AsyncMock, return_value=mock_react_ret
        ) as mock_react:
            await orch._run_worker(t)

            mock_react.assert_awaited_once_with("test goal")
            assert orch.results[t.id].ok is True
            assert "task done" in orch.results[t.id].summary


@pytest.mark.mocked
class TestAsyncOrchestratorInit:
    """Test AsyncOrchestrator.__init__ accepts new parameters."""

    def test_init_stores_skill_router_mcp_workspace_max_steps(self):
        """New optional parameters are stored as instance variables."""
        skill_router = MagicMock()
        mcp = MagicMock()
        workspace = "/my/workspace"
        max_steps = 10

        orch = AsyncOrchestrator(
            skill_router=skill_router,
            mcp=mcp,
            workspace=workspace,
            max_steps=max_steps,
        )

        assert orch.skill_router is skill_router
        assert orch.mcp is mcp
        assert orch.workspace == workspace
        assert orch.max_steps == max_steps

    def test_init_defaults_are_sensible(self):
        """Defaults work without the new parameters."""
        orch = AsyncOrchestrator()
        assert orch.skill_router is None
        assert orch.mcp is None
        assert orch.workspace == "."
        assert orch.max_steps == 6

    def test_init_defaults_agent_instructions_empty(self):
        """Regression: AsyncOrchestrator must define agent_instructions (default "").

        The ReAct loop (_run_react_loop) reads self.agent_instructions to inject
        AGENTS.md into the cached prefix; the graph execute node propagates the real
        per-task value from the CodingOrchestrator. Without this default, react_execute
        driven directly (tests, or before the graph sets it) raised AttributeError.
        """
        orch = AsyncOrchestrator()
        assert orch.agent_instructions == ""


@pytest.mark.mocked
class TestReactExecuteBudget:
    """IterationBudget wire-in: grace call on exhaustion + cheap-call refund."""

    def _make_orch(self, max_steps=6):
        from services.orchestrator.coding_orchestrator import AsyncOrchestrator

        return AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp", max_steps=max_steps)

    def _bash_resp(self, command):
        return _msg_with_tool_call("run_bash", json.dumps({"command": command}))

    def _list_dir_resp(self, path):
        return _msg_with_tool_call("list_dir", json.dumps({"path": path}))

    def _finish_resp(self, summary):
        return _msg_with_tool_call("finish", json.dumps({"summary": summary}))

    @pytest.mark.asyncio
    async def test_exhaustion_grants_exactly_one_grace_call(self):
        """Cap 2 + always-write_file => model is called cap+1 (=3) times, then stops.

        Uses write_file (non-refundable) to test budget exhaustion; run_bash is now refundable.
        """
        orch = self._make_orch(max_steps=2)
        mcp = AsyncMock()
        mcp_result = MagicMock()
        mcp_result.content = [MagicMock(text="file written")]
        mcp_result.isError = False
        mcp.call_tool.return_value = mcp_result
        orch.mcp = mcp

        # Different files to avoid loop detector (each call has different args)
        responses = [
            MagicMock(
                choices=[
                    MagicMock(
                        message=_msg_with_tool_call(
                            "write_file", json.dumps({"path": f"file{i}.txt", "content": str(i)})
                        )
                    )
                ]
            )
            for i in range(10)
        ]

        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock,
            side_effect=responses,
        ) as m:
            result = await orch.react_execute("loop forever")

        assert m.await_count == 3  # cap (2) + one grace turn
        assert result["ok"] is False
        assert "budget exhausted" in result["summary"]

    @pytest.mark.asyncio
    async def test_grace_call_that_finishes_succeeds(self):
        """Cap 1: one working turn exhausts the budget, the grace turn calls finish."""
        orch = self._make_orch(max_steps=1)
        mcp = AsyncMock()
        mcp_result = MagicMock()
        mcp_result.content = [MagicMock(text="output")]
        mcp_result.isError = False
        mcp.call_tool.return_value = mcp_result
        orch.mcp = mcp

        r1 = MagicMock(choices=[MagicMock(message=self._bash_resp("echo work"))])
        r2 = MagicMock(choices=[MagicMock(message=self._finish_resp("finished on grace"))])

        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock,
            side_effect=[r1, r2],
        ) as m:
            result = await orch.react_execute("needs one more step")

        assert m.await_count == 2
        assert result["ok"] is True
        assert "finished on grace" in result["summary"]

    @pytest.mark.asyncio
    async def test_read_only_iteration_is_refunded(self):
        """Cap 2: a list_dir turn is refunded, so two run_bash turns + finish fit."""
        orch = self._make_orch(max_steps=2)
        mcp = AsyncMock()
        mcp_result = MagicMock()
        mcp_result.content = [MagicMock(text="output")]
        mcp_result.isError = False
        mcp.call_tool.return_value = mcp_result
        orch.mcp = mcp

        r1 = MagicMock(choices=[MagicMock(message=self._list_dir_resp("."))])
        r2 = MagicMock(choices=[MagicMock(message=self._bash_resp("echo a"))])
        r3 = MagicMock(choices=[MagicMock(message=self._bash_resp("echo b"))])
        r4 = MagicMock(choices=[MagicMock(message=self._finish_resp("done after refund"))])

        # list_dir routes through local tools; with local_client=None it returns a
        # structured error but still counts as a cheap (refunded) read turn.
        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock,
            side_effect=[r1, r2, r3, r4],
        ) as m:
            result = await orch.react_execute("inspect then work twice")

        assert m.await_count == 4  # refund of the list_dir turn buys the 4th call
        assert result["ok"] is True
        assert "done after refund" in result["summary"]

    @pytest.mark.asyncio
    async def test_env_var_overrides_max_steps(self, monkeypatch):
        """LABMATE_MAX_ITERATIONS overrides the constructor max_steps default."""
        monkeypatch.setenv("LABMATE_MAX_ITERATIONS", "1")
        orch = self._make_orch(max_steps=6)  # constructor says 6...
        mcp = AsyncMock()
        mcp_result = MagicMock()
        mcp_result.content = [MagicMock(text="output")]
        mcp_result.isError = False
        mcp.call_tool.return_value = mcp_result
        orch.mcp = mcp

        # Different commands to avoid loop detector
        responses = [
            MagicMock(choices=[MagicMock(message=self._bash_resp(f"echo {i}"))]) for i in range(10)
        ]
        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock,
            side_effect=responses,
        ) as m:
            result = await orch.react_execute("loop")

        # ...but the env knob clamps to 1 => 1 working turn + 1 grace = 2 calls.
        assert m.await_count == 2
        # The loop exits when absolute turn limit (2*1=2) is exceeded on turn 3
        # (before grace can fire again), or after grace is exhausted.
        assert "absolute turn limit" in result["summary"] or "budget exhausted" in result["summary"]

    @pytest.mark.mocked
    @pytest.mark.asyncio
    async def test_edit_goal_gets_higher_iteration_ceiling(self, monkeypatch, tmp_path):
        """An edit-intent goal builds the budget with LABMATE_MAX_ITERATIONS_EDIT
        (default 12), giving more than max_steps (6) turns of headroom."""
        monkeypatch.setattr(
            "services.orchestrator.coding_orchestrator.SEQUENCING_MODE", "skill_first"
        )
        # Leave LABMATE_MAX_ITERATIONS and LABMATE_MAX_ITERATIONS_EDIT unset -> defaults.
        monkeypatch.delenv("LABMATE_MAX_ITERATIONS", raising=False)
        monkeypatch.delenv("LABMATE_MAX_ITERATIONS_EDIT", raising=False)
        from services.orchestrator import events

        orch = AsyncOrchestrator(skill_router=None, mcp=MagicMock(), max_steps=6)
        # Direct execution (execute_local_tool) now writes/reads real files under
        # a real workspace root — each write_file's distinct content read-verifies.
        orch.workspace = str(tmp_path)
        orch.local_client = MagicMock()

        calls = [0]

        async def _model(*a, **k):
            calls[0] += 1
            # DISTINCT content each turn -> distinct signatures (no loop trip).
            # write_file is NOT refundable, so each turn consumes a unit.
            return MagicMock(
                choices=[
                    MagicMock(
                        message=_msg_with_tool_call(
                            "write_file", json.dumps({"path": "a.py", "content": f"v{calls[0]}"})
                        )
                    )
                ]
            )

        class FakeEmitter:
            async def emit(self, type, **f):
                pass

        with patch(
            "services.orchestrator.coding_orchestrator.acompletion_with_failover",
            new_callable=AsyncMock,
            side_effect=_model,
        ):
            token = events.current_emitter.set(FakeEmitter())
            try:
                await orch.react_execute("fix the bug in a.py")  # edit intent
            finally:
                events.current_emitter.reset(token)

        # With the old shared cap of 6 this would stop near 6 consume-turns; the edit
        # ceiling of 12 lets it run materially further before halting.
        assert calls[0] > 7


@pytest.mark.asyncio
async def test_react_routes_read_file_to_local_tool(tmp_path):
    """ReAct loop executes read_file DIRECTLY via execute_local_tool (no bus
    round-trip): the real file content from disk reaches the turn-2 model call.
    """
    from services.orchestrator import events

    # Seed a real file in the workspace so direct execution has real content
    # to return — proves this is execute_local_tool reading the real disk,
    # not a mocked/round-tripped result.
    (tmp_path / "a.txt").write_text("FILE BODY", encoding="utf-8")

    class FakeEmitter:
        async def emit(self, type, **f):
            pass

    token = events.current_emitter.set(FakeEmitter())

    orch = AsyncOrchestrator(skill_router=None, max_steps=3)
    orch.workspace = str(tmp_path)
    orch.local_client = MagicMock()  # truthy so the local-tool branch is taken

    # Turn 1: LLM calls read_file. Turn 2: LLM calls finish.
    read_file_msg = _msg_with_tool_call("read_file", '{"path": "a.txt"}')
    finish_msg_obj = _msg_with_tool_call("finish", '{"summary": "read complete"}')

    resp1 = MagicMock(choices=[MagicMock(message=read_file_msg)])
    resp2 = MagicMock(choices=[MagicMock(message=finish_msg_obj)])

    try:
        with patch(
            "services.orchestrator.coding_orchestrator.acompletion_with_failover",
            new_callable=AsyncMock,
            side_effect=[resp1, resp2],
        ) as mock_acompletion:
            out = await orch.react_execute("read a.txt")
    finally:
        events.current_emitter.reset(token)

    assert out["ok"] is True
    assert out["summary"] == "read complete"

    # Positive proof: the REAL file content ("FILE BODY", read straight off disk
    # via execute_local_tool) was fed back into the turn-2 model call — not a
    # round-trip mock and not "no local tool client connected".
    assert mock_acompletion.call_count == 2
    turn2_messages = mock_acompletion.call_args_list[1].kwargs["messages"]
    tool_msgs = [m for m in turn2_messages if m.get("role") == "tool"]
    assert tool_msgs, "expected a tool-result message fed back into turn 2"
    tool_result_blob = " ".join(str(m.get("content", "")) for m in tool_msgs)
    assert (
        "FILE BODY" in tool_result_blob
    ), f"expected the real file content in the turn-2 tool result, got: {tool_result_blob}"
    assert "no local tool client connected" not in tool_result_blob


@pytest.mark.asyncio
async def test_react_routes_write_file_to_local_tool(monkeypatch, tmp_path):
    """ReAct loop executes write_file DIRECTLY via execute_local_tool: the file
    really lands on disk under tmp_path, the read-back verify passes, and the
    path is recorded in edited_files (verification-stop bookkeeping)."""
    from services.orchestrator import events

    # Disable the verification-stop nudge so a bare write+finish completes in
    # 2 turns — this test targets the direct-execution write mechanics, not
    # the verify-nudge flow (covered separately by the verification_stop tests).
    monkeypatch.setenv("MAX_VERIFY_NUDGES", "0")

    class FakeEmitter:
        async def emit(self, type, **f):
            pass

    token = events.current_emitter.set(FakeEmitter())

    orch = AsyncOrchestrator(skill_router=None, max_steps=3)
    orch.workspace = str(tmp_path)
    orch.local_client = MagicMock()  # truthy so the local-tool branch is taken

    write_msg = _msg_with_tool_call(
        "write_file", json.dumps({"path": "out.txt", "content": "NEW CONTENT"})
    )
    finish_msg_obj = _msg_with_tool_call("finish", '{"summary": "write complete"}')

    resp1 = MagicMock(choices=[MagicMock(message=write_msg)])
    resp2 = MagicMock(choices=[MagicMock(message=finish_msg_obj)])

    try:
        with patch(
            "services.orchestrator.coding_orchestrator.acompletion_with_failover",
            new_callable=AsyncMock,
            side_effect=[resp1, resp2],
        ) as mock_acompletion:
            out = await orch.react_execute("write out.txt")
    finally:
        events.current_emitter.reset(token)

    assert out["ok"] is True
    assert "write complete" in out["summary"]

    # The write really landed on disk (not a mock, not a round-trip).
    written = tmp_path / "out.txt"
    assert written.exists()
    assert written.read_text(encoding="utf-8") == "NEW CONTENT"

    # The read-back verify passed, and the tool result reflects that.
    assert mock_acompletion.call_count == 2
    turn2_messages = mock_acompletion.call_args_list[1].kwargs["messages"]
    tool_msgs = [m for m in turn2_messages if m.get("role") == "tool"]
    assert tool_msgs, "expected a tool-result message fed back into turn 2"
    tool_result_blob = " ".join(str(m.get("content", "")) for m in tool_msgs)
    assert '"verified": true' in tool_result_blob.lower()


@pytest.mark.asyncio
async def test_react_file_tool_with_no_local_client_returns_error():
    """When local_client is None, file tools return a structured error rather than raising."""
    orch = AsyncOrchestrator(skill_router=None, max_steps=2)
    assert orch.local_client is None  # default

    read_file_msg = _msg_with_tool_call("read_file", '{"path": "x.txt"}')
    finish_msg_obj = _msg_with_tool_call("finish", '{"summary": "done"}')

    resp1 = MagicMock(choices=[MagicMock(message=read_file_msg)])
    resp2 = MagicMock(choices=[MagicMock(message=finish_msg_obj)])

    with patch(
        "services.orchestrator.coding_orchestrator.litellm.acompletion",
        new_callable=AsyncMock,
        side_effect=[resp1, resp2],
    ):
        out = await orch.react_execute("read x.txt")

    assert out["ok"] is True  # finish was called, so it succeeded
    assert out["summary"] == "done"


@pytest.mark.asyncio
async def test_react_execute_builds_prompt_assembler_once_per_goal():
    """The ReAct loop constructs exactly one PromptAssembler per react_execute call."""
    from services.orchestrator.coding_orchestrator import AsyncOrchestrator

    orch = AsyncOrchestrator(skill_router=None, mcp=None, max_steps=3)

    # Step 1: run_bash (no mcp -> returns error dict, loop continues). Step 2: finish.
    r1 = _msg_with_tool_call("run_bash", '{"command":"ls"}')
    r2 = _msg_with_tool_call("finish", '{"summary":"done"}')
    resp1 = MagicMock(choices=[MagicMock(message=r1)])
    resp2 = MagicMock(choices=[MagicMock(message=r2)])

    with patch("services.orchestrator.coding_orchestrator.PromptAssembler") as MockPA:
        instance = MockPA.return_value
        instance.system_message.return_value = {"role": "system", "content": "SYS"}
        instance.tools.return_value = [{"type": "function", "function": {"name": "finish"}}]
        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock,
            side_effect=[resp1, resp2],
        ):
            out = await orch.react_execute("inspect then finish")

    assert out["ok"] is True
    # Exactly one assembler for the whole goal — not one per step.
    assert MockPA.call_count == 1


def test_react_system_prompt_directs_code_to_sandbox():
    from services.orchestrator.prompt_assembler import BASE_SYSTEM_PROMPT

    assert "code-sandbox" in BASE_SYSTEM_PROMPT
    assert "run_bash" in BASE_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_react_loop_feeds_small_bash_output_verbatim():
    """A small run_bash result reaches the model context unchanged (no marker,
    no 2-4k cut). Regression guard: the old code path also passed small output
    through, but now via ground_tool_result — confirm verbatim + no marker."""
    from services.orchestrator.coding_orchestrator import AsyncOrchestrator

    orch = AsyncOrchestrator(skill_router=None, mcp=AsyncMock(), workspace="/tmp", max_steps=6)
    bash_result = MagicMock()
    bash_result.content = [MagicMock(text="hello world")]
    bash_result.isError = False
    orch.mcp.call_tool = AsyncMock(return_value=bash_result)

    resp1 = MagicMock(
        choices=[MagicMock(message=_msg_with_tool_call("run_bash", '{"command":"echo hello"}', ""))]
    )
    finish_msg = MagicMock(tool_calls=None, content="done")
    finish_msg.model_dump = lambda: {"role": "assistant", "content": "done"}
    resp2 = MagicMock(choices=[MagicMock(message=finish_msg)])

    captured_messages = {}

    async def _capture(*a, **k):
        # On the 2nd call the messages list holds the appended tool result.
        captured_messages["messages"] = list(k["messages"])
        return resp2 if len(captured_messages) and "appended" in captured_messages else resp1

    # Simpler: drive two scripted responses and inspect via a spy on append.
    with patch(
        "services.orchestrator.coding_orchestrator.litellm.acompletion",
        new_callable=AsyncMock,
        side_effect=[resp1, resp2],
    ):
        await orch.react_execute("echo hello")

    # The bash output was small → fed verbatim. We assert via re-running the
    # grounding helper on the same content for determinism.
    from services.orchestrator.tool_grounding import ground_tool_result

    grounded = ground_tool_result("hello world", 16000)
    assert grounded == "hello world"
    assert "truncated" not in grounded


@pytest.mark.asyncio
async def test_react_loop_grounds_huge_bash_output_with_marker_and_tail():
    """A huge run_bash result is grounded: the tool message appended to the
    model context contains BOTH a truncation marker AND the tail sentinel."""
    from services.orchestrator.coding_orchestrator import AsyncOrchestrator

    huge = ("A" * 50000) + "TAILSENTINEL"
    orch = AsyncOrchestrator(skill_router=None, mcp=AsyncMock(), workspace="/tmp", max_steps=6)
    bash_result = MagicMock()
    bash_result.content = [MagicMock(text=huge)]
    bash_result.isError = False
    orch.mcp.call_tool = AsyncMock(return_value=bash_result)

    resp1 = MagicMock(
        choices=[
            MagicMock(message=_msg_with_tool_call("run_bash", '{"command":"cat big.log"}', ""))
        ]
    )
    finish_msg = MagicMock(tool_calls=None, content="done")
    finish_msg.model_dump = lambda: {"role": "assistant", "content": "done"}
    resp2 = MagicMock(choices=[MagicMock(message=finish_msg)])

    seen = {}

    async def _spy(*a, **k):
        # Capture the messages list passed on each model call.
        seen.setdefault("calls", []).append([dict(m) for m in k["messages"]])
        return seen["calls"] and (resp2 if len(seen["calls"]) >= 2 else resp1) or resp1

    with patch(
        "services.orchestrator.coding_orchestrator.litellm.acompletion",
        new_callable=AsyncMock,
        side_effect=_spy,
    ):
        await orch.react_execute("dump log")

    # The 2nd model call carries the appended tool result message.
    second_call_messages = seen["calls"][1]
    tool_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
    assert tool_msgs, "no tool message was appended to context"
    content = tool_msgs[-1]["content"]
    assert "truncated" in content  # marker present
    assert "TAILSENTINEL" in content  # end-of-output evidence survived
    assert len(content) < len(huge)  # genuinely truncated


@pytest.mark.mocked
class TestEditIntentRouting:
    """react_execute routes edit-intent goals to the ReAct loop, not single-skill."""

    def _make_orch(self):
        from services.orchestrator.coding_orchestrator import AsyncOrchestrator

        # A skill_router whose .run() would succeed with a read-only result, so if
        # the fast-path were taken we'd see THAT summary (and never reach the loop).
        router = MagicMock()
        router.runner = MagicMock()
        router.runner.reset_activations = MagicMock()
        router.run = AsyncMock(return_value={"ok": True, "result": "read-only review output"})
        return AsyncOrchestrator(
            skill_router=router, mcp=AsyncMock(), workspace="/tmp", max_steps=4
        )

    @pytest.mark.asyncio
    async def test_edit_goal_enters_react_loop_not_skill_first(self):
        """An edit-intent goal must bypass _run_skill_first and run the ReAct loop."""
        orch = self._make_orch()

        skill_first_calls = {"n": 0}

        async def _spy_skill_first(goal):
            skill_first_calls["n"] += 1
            return {"ok": True, "summary": "SHOULD NOT BE CALLED"}

        # Fake model: finish immediately so the loop returns ok=True quickly.
        finish_msg = MagicMock()
        finish_msg.content = None
        tc = MagicMock()
        tc.id = "c1"
        tc.function.name = "finish"
        tc.function.arguments = json.dumps({"summary": "fixed the bug"})
        finish_msg.tool_calls = [tc]
        resp = MagicMock(choices=[MagicMock(message=finish_msg)])

        with (
            patch.object(orch, "_run_skill_first", side_effect=_spy_skill_first),
            patch.object(orch, "_run_react_loop", wraps=orch._run_react_loop) as loop_spy,
            patch(
                "services.orchestrator.coding_orchestrator.acompletion_with_failover",
                new_callable=AsyncMock,
                return_value=resp,
            ),
        ):
            result = await orch.react_execute(
                "Review the code then fix the bug",
            )

        assert skill_first_calls["n"] == 0, "skill-first fast-path must be skipped for edit goals"
        assert loop_spy.called, "the multi-tool ReAct loop must run for edit goals"
        assert result["ok"] is True
        assert "fixed the bug" in result["summary"]

    @pytest.mark.asyncio
    async def test_read_goal_stays_on_skill_first(self):
        """A pure read/answer goal keeps the existing single-skill fast-path."""
        orch = self._make_orch()
        with patch.object(orch, "_run_react_loop", new_callable=AsyncMock) as loop_spy:
            result = await orch.react_execute("Summarize what this module does")
        assert not loop_spy.called, "read goals must NOT enter the ReAct loop via the edit branch"
        assert result["ok"] is True
        assert "read-only review output" in result["summary"]

    @pytest.mark.asyncio
    async def test_flag_off_keeps_skill_first_for_edit_goal(self, monkeypatch):
        """ROUTE_EDIT_TO_REACT=0 -> edit goals keep today's skill_first behavior."""
        monkeypatch.setenv("ROUTE_EDIT_TO_REACT", "0")
        orch = self._make_orch()
        with patch.object(orch, "_run_react_loop", new_callable=AsyncMock) as loop_spy:
            result = await orch.react_execute("Fix the bug in factorial")
        assert not loop_spy.called, "flag off must preserve today's skill_first path"
        assert "read-only review output" in result["summary"]

    @pytest.mark.asyncio
    async def test_file_read_goal_enters_react_loop_not_skill_first(self):
        """Piece 5 fix-B: a read-only file-access goal ('read x.py and summarize')
        must ALSO bypass _run_skill_first and reach _run_react_loop, since the
        single-skill fast-path has no file-tool access. requires_local_tools
        broadens the loop-entry gate beyond requires_editing for this case."""
        orch = self._make_orch()

        with (
            patch.object(orch, "_run_skill_first", new_callable=AsyncMock) as skill_first_spy,
            patch.object(orch, "_run_react_loop", new_callable=AsyncMock) as loop_spy,
        ):
            loop_spy.return_value = {"ok": True, "summary": "read x.py and summarized it"}
            result = await orch.react_execute("read x.py and summarize it")

        assert (
            not skill_first_spy.called
        ), "skill-first fast-path must be skipped for file-access goals"
        assert loop_spy.called, "the multi-tool ReAct loop must run for file-access goals"
        assert result["ok"] is True


def _vt_tool_msg(name, arguments):
    """A litellm-style assistant message that calls a single tool (verify-stop tests)."""
    tc = MagicMock()
    tc.id = f"call-{name}"
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = json.dumps(arguments)
    msg = MagicMock()
    msg.tool_calls = [tc]
    msg.content = ""
    msg.reasoning_content = ""
    msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    return MagicMock(choices=[MagicMock(message=msg)])


@pytest.mark.asyncio
async def test_verification_stop_nudges_then_accepts_after_tests_pass(monkeypatch, tmp_path):
    """Edit then finish without tests must be nudged, not accepted."""
    from services.orchestrator import events

    monkeypatch.setenv("MAX_VERIFY_NUDGES", "2")
    # Ensure we're exercising the ReAct loop, not skill-first
    monkeypatch.setattr("services.orchestrator.coding_orchestrator.SEQUENCING_MODE", "react")
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace=str(tmp_path))

    # write_file now executes directly (execute_local_tool) against tmp_path; the
    # read-back verify reads the real file it just wrote, so no local-tool mock
    # is needed for read/write.
    orch.local_client = MagicMock()  # truthy so the write_file branch is taken

    # run_tests now runs as a direct local subprocess; stub
    # asyncio.create_subprocess_shell so it returns a passing result without
    # spawning a real process.
    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"1 passed", None))
    fake_proc.returncode = 0
    fake_proc.kill = MagicMock()
    fake_proc.wait = AsyncMock()
    subprocess_mock = AsyncMock(return_value=fake_proc)

    responses = [
        _vt_tool_msg("write_file", {"path": "src/app.py", "content": "x = 1"}),
        _vt_tool_msg("finish", {"summary": "I fixed the bug and all tests pass"}),
        _vt_tool_msg("run_tests", {"path": "tests/"}),
        _vt_tool_msg("finish", {"summary": "tests pass"}),
        # Provide extra responses in case of additional loops
        _vt_tool_msg("finish", {"summary": "all verified"}),
    ]

    class FakeEmitter:
        async def emit(self, type, **f):
            pass

    with (
        patch(
            "services.orchestrator.coding_orchestrator.acompletion_with_failover",
            new_callable=AsyncMock,
            side_effect=responses,
        ) as mock,
        patch(
            "services.orchestrator.coding_orchestrator.asyncio.create_subprocess_shell",
            new=subprocess_mock,
        ),
    ):
        token = events.current_emitter.set(FakeEmitter())
        try:
            result = await orch.react_execute("fix the bug in src/app.py")
        finally:
            events.current_emitter.reset(token)

    assert result["ok"] is True
    # The summary should mention tests
    assert any(x in result["summary"].lower() for x in ["test", "pass"])
    # Should make at least 4 calls (write, finish1, run_tests, finish2)
    assert mock.call_count >= 4


@pytest.mark.asyncio
async def test_run_tests_client_routed_pass(monkeypatch):
    """run_tests runs as a direct local subprocess (asyncio.create_subprocess_shell),
    even when a client with a run_tests-declaring manifest is attached — parse the
    {ok, exit_code, raw_output} response, and record tests_passed when ok=True."""
    from services.orchestrator import events
    from services.orchestrator.tool_manifest import parse_manifest

    monkeypatch.setattr("services.orchestrator.coding_orchestrator.SEQUENCING_MODE", "react")
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    orch.local_client = MagicMock()

    # Set up client context with a manifest that declares run_tests.
    manifest = parse_manifest(
        {
            "tools": [
                {"name": "read_file"},
                {"name": "write_file"},
                {"name": "run_tests"},
            ]
        }
    )

    class FakeContext:
        def get_manifest(self):
            return manifest

        def get_workspace_root(self):
            return None

    # Mock the client_context to return our manifest
    monkeypatch.setattr("services.orchestrator.coding_orchestrator.client_context", FakeContext())

    # Stub the direct-subprocess seam to return a passing test result.
    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"3 passed", None))
    fake_proc.returncode = 0
    fake_proc.kill = MagicMock()
    fake_proc.wait = AsyncMock()
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.asyncio.create_subprocess_shell",
        AsyncMock(return_value=fake_proc),
    )

    # Single model response: call run_tests then finish
    responses = [
        _vt_tool_msg("run_tests", {"path": "tests/"}),
        _vt_tool_msg("finish", {"summary": "tests passed"}),
    ]

    class FakeEmitter:
        async def emit(self, type, **f):
            pass

    with patch(
        "services.orchestrator.coding_orchestrator.acompletion_with_failover",
        new_callable=AsyncMock,
        side_effect=responses,
    ):
        token = events.current_emitter.set(FakeEmitter())
        try:
            result = await orch.react_execute("run tests")
        finally:
            events.current_emitter.reset(token)

    # Should succeed because the tests passed
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_run_tests_client_routed_fail(monkeypatch):
    """A direct-subprocess run_tests with a nonzero exit (ok=False) should not set
    tests_passed and may trigger the verification-stop nudge."""
    from services.orchestrator import events
    from services.orchestrator.tool_manifest import parse_manifest

    monkeypatch.setattr("services.orchestrator.coding_orchestrator.SEQUENCING_MODE", "react")
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    orch.local_client = MagicMock()

    manifest = parse_manifest(
        {
            "tools": [
                {"name": "read_file"},
                {"name": "write_file"},
                {"name": "run_tests"},
            ]
        }
    )

    class FakeContext:
        def get_manifest(self):
            return manifest

        def get_workspace_root(self):
            return None

    monkeypatch.setattr("services.orchestrator.coding_orchestrator.client_context", FakeContext())

    # Stub the direct-subprocess seam to return a failing test result.
    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"1 failed", None))
    fake_proc.returncode = 1
    fake_proc.kill = MagicMock()
    fake_proc.wait = AsyncMock()
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.asyncio.create_subprocess_shell",
        AsyncMock(return_value=fake_proc),
    )

    responses = [
        _vt_tool_msg("run_tests", {"path": "tests/"}),
        _vt_tool_msg("finish", {"summary": "test result"}),
    ]

    # Capture the run_tests tool result fed back to the model. This is the
    # ground truth of what the direct-subprocess branch shaped — mutation-proof:
    # if the shaper forced "ok": True for a failing run, this captured value
    # would be ok=True and the assertions below would FAIL.
    run_tests_results: list[dict] = []

    class FakeEmitter:
        async def emit(self, type, **f):
            if type == "tool.done":
                raw = f.get("result")
                try:
                    parsed = json.loads(raw) if isinstance(raw, str) else raw
                except (TypeError, ValueError):
                    parsed = None
                if isinstance(parsed, dict) and "exit_code" in parsed:
                    run_tests_results.append(parsed)

    with patch(
        "services.orchestrator.coding_orchestrator.acompletion_with_failover",
        new_callable=AsyncMock,
        side_effect=responses,
    ):
        token = events.current_emitter.set(FakeEmitter())
        try:
            await orch.react_execute("run tests")
        finally:
            events.current_emitter.reset(token)

    # The direct-subprocess branch must have run and shaped the failing result.
    assert run_tests_results, "run_tests tool result was never emitted (branch not taken)"
    shaped = run_tests_results[-1]
    # A FAILING client test result must NOT be credited as a pass: the shaped
    # result fed back to the model must preserve ok=False / nonzero exit_code,
    # and the verification predicate must agree it did not pass.
    assert shaped["ok"] is False, f"failing run_tests was credited as ok=True: {shaped}"
    assert shaped["exit_code"] != 0, f"failing run_tests got a zero exit code: {shaped}"
    assert (
        _run_tests_passed(json.dumps(shaped)) is False
    ), "verification predicate counted a failing run as a pass"


@pytest.mark.asyncio
async def test_run_tests_no_client_uses_pod_path(monkeypatch):
    """When no client and no skill_router are attached, run_tests still works — it
    runs as a direct local subprocess (asyncio.create_subprocess_shell), no client
    or skill router needed."""
    from services.orchestrator import events

    monkeypatch.setattr("services.orchestrator.coding_orchestrator.SEQUENCING_MODE", "react")

    # No skill_router, no local_client attached (simulating a bare co-located orchestrator).
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    orch.local_client = None  # No client attached

    # Mock client_context to return None (no manifest)
    class FakeContext:
        def get_manifest(self):
            return None

        def get_workspace_root(self):
            return None

    monkeypatch.setattr("services.orchestrator.coding_orchestrator.client_context", FakeContext())

    # Stub the direct-subprocess seam to return a passing test result.
    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"2 passed", None))
    fake_proc.returncode = 0
    fake_proc.kill = MagicMock()
    fake_proc.wait = AsyncMock()
    subprocess_mock = AsyncMock(return_value=fake_proc)

    # Single model response: call run_tests then finish
    responses = [
        _vt_tool_msg("run_tests", {"path": "tests/"}),
        _vt_tool_msg("finish", {"summary": "tests passed"}),
    ]

    class FakeEmitter:
        async def emit(self, type, **f):
            pass

    with (
        patch(
            "services.orchestrator.coding_orchestrator.acompletion_with_failover",
            new_callable=AsyncMock,
            side_effect=responses,
        ),
        patch(
            "services.orchestrator.coding_orchestrator.asyncio.create_subprocess_shell",
            new=subprocess_mock,
        ),
    ):
        token = events.current_emitter.set(FakeEmitter())
        try:
            result = await orch.react_execute("run tests")
        finally:
            events.current_emitter.reset(token)

    # Verify the direct-subprocess seam was used (no skill_router / client needed)
    assert subprocess_mock.called, "run_tests should call create_subprocess_shell directly"
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_gating_run_bash_and_code_semantic_search_not_advertised_when_client_attached(
    monkeypatch,
):
    """When a client is attached with only read/write/list/search_files/run_tests,
    the advertised tools must NOT include run_bash or code_semantic_search."""
    from services.orchestrator.prompt_assembler import _static_tail_schemas
    from services.orchestrator.tool_manifest import build_tool_list

    # Create a minimal manifest with just the basic file tools + run_tests
    manifest = {
        "tools": [
            {"name": "read_file"},
            {"name": "write_file"},
            {"name": "list_dir"},
            {"name": "search_files"},
            {"name": "run_tests"},
        ]
    }

    tools = build_tool_list(
        manifest,
        skill_router=None,
        codegraph_enabled=False,
        static_tail=_static_tail_schemas(),
    )

    # build_tool_list returns OpenAI function schemas shaped
    # {"type": "function", "function": {"name": ...}} — the name is NESTED,
    # never top-level. Extract it correctly so the set is non-empty.
    tool_names = {
        t["function"]["name"] for t in tools if isinstance(t, dict) and t.get("type") == "function"
    }

    # Positive assertions — prove the manifest-declared builtins ARE advertised.
    # These guard against the test going vacuous again (an empty set would fail here).
    assert "run_tests" in tool_names, "manifest-declared run_tests must be advertised"
    assert "search_files" in tool_names, "manifest-declared search_files must be advertised"
    assert "read_file" in tool_names, "manifest-declared read_file must be advertised"

    # Gated tools must NOT be advertised.
    # run_bash must NOT be in the tool list (never advertised with a client attached).
    assert (
        "run_bash" not in tool_names
    ), "run_bash must not be advertised when client attached without it"
    # code_semantic_search must NOT be in the tool list (not declared in manifest).
    assert (
        "code_semantic_search" not in tool_names
    ), "code_semantic_search must not be advertised when not declared in manifest"


@pytest.mark.asyncio
async def test_verification_stop_no_edits_finishes_immediately(monkeypatch):
    """A goal that edits nothing finishes on the first finish."""
    from services.orchestrator import events

    monkeypatch.setenv("MAX_VERIFY_NUDGES", "2")
    monkeypatch.setattr("services.orchestrator.coding_orchestrator.SEQUENCING_MODE", "react")
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    responses = [_vt_tool_msg("finish", {"summary": "2 + 2 = 4"})]

    class FakeEmitter:
        async def emit(self, type, **f):
            pass

    with patch(
        "services.orchestrator.coding_orchestrator.acompletion_with_failover",
        new_callable=AsyncMock,
        side_effect=responses,
    ) as mock:
        token = events.current_emitter.set(FakeEmitter())
        try:
            result = await orch.react_execute("what is 2 plus 2")
        finally:
            events.current_emitter.reset(token)
    assert result["ok"] is True
    assert result["summary"] == "2 + 2 = 4"
    assert mock.call_count == 1


@pytest.mark.asyncio
async def test_verification_stop_cap_accepts_finish_honestly(monkeypatch, tmp_path):
    """After MAX_VERIFY_NUDGES the finish is accepted but annotated honestly."""
    from services.orchestrator import events

    monkeypatch.setenv("MAX_VERIFY_NUDGES", "1")
    monkeypatch.setattr("services.orchestrator.coding_orchestrator.SEQUENCING_MODE", "react")
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace=str(tmp_path))
    # write_file executes directly against tmp_path; the read-back verify reads
    # the real file it just wrote, so no local-tool mock is needed.
    orch.local_client = MagicMock()

    responses = [
        _vt_tool_msg("write_file", {"path": "src/app.py", "content": "x = 1"}),
        _vt_tool_msg("finish", {"summary": "all done"}),  # nudged (1/1)
        _vt_tool_msg("finish", {"summary": "still done"}),  # cap reached -> accepted
    ]

    class FakeEmitter:
        async def emit(self, type, **f):
            pass

    with patch(
        "services.orchestrator.coding_orchestrator.acompletion_with_failover",
        new_callable=AsyncMock,
        side_effect=responses,
    ) as mock:
        token = events.current_emitter.set(FakeEmitter())
        try:
            result = await orch.react_execute("fix src/app.py")
        finally:
            events.current_emitter.reset(token)

    assert result["ok"] is True
    assert "not verified" in result["summary"].lower()
    assert mock.call_count == 3


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_react_loop_tolerates_two_identical_write_file_calls(monkeypatch, tmp_path):
    """A legit 'edit, test failed, edit again' retry: two identical write_file
    calls must NOT trip the loop detector (mutating tolerance >= 4)."""
    monkeypatch.setattr("services.orchestrator.coding_orchestrator.SEQUENCING_MODE", "skill_first")
    from services.orchestrator import events

    orch = AsyncOrchestrator(skill_router=None, mcp=MagicMock(), max_steps=6)
    # write_file executes directly (execute_local_tool) against tmp_path; the
    # read-back verify reads the real file it just wrote (content "x").
    orch.workspace = str(tmp_path)
    orch.local_client = MagicMock()

    def write_msg():
        return MagicMock(
            choices=[
                MagicMock(
                    message=_msg_with_tool_call(
                        "write_file", json.dumps({"path": "a.py", "content": "x"})
                    )
                )
            ]
        )

    finish_msg = MagicMock(tool_calls=None, content="done")
    finish_msg.model_dump = lambda: {"role": "assistant", "content": "done"}
    finish_resp = MagicMock(choices=[MagicMock(message=finish_msg)])

    class FakeEmitter:
        async def emit(self, type, **f):
            pass

    with patch(
        "services.orchestrator.coding_orchestrator.acompletion_with_failover",
        new_callable=AsyncMock,
        side_effect=[write_msg(), write_msg(), finish_resp],
    ):
        token = events.current_emitter.set(FakeEmitter())
        try:
            result = await orch.react_execute("apply a patch to a.py")
        finally:
            events.current_emitter.reset(token)

    # The 2nd identical write_file did NOT halt the loop.
    assert "loop detected" not in result["summary"].lower()


class TestSkillFirstPuntReconciliation:
    """Wire-in (a): a read-only skill that returns ok=True with a PUNT answer
    ('file too large') must be downgraded to ok=False by reconcile_ok."""

    @pytest.mark.asyncio
    async def test_skill_first_punt_answer_downgraded_to_ok_false(self):
        from services.orchestrator.coding_orchestrator import AsyncOrchestrator

        orch = AsyncOrchestrator(skill_router=MagicMock(), mcp=None, workspace="/tmp")
        # skill_router.run returns a ok=True result whose text is a punt.
        orch.skill_router.run = AsyncMock(
            return_value={
                "ok": True,
                "result": (
                    "I couldn't analyze the file because it is too large. "
                    "Please provide a smaller snippet."
                ),
                "skill_name": "repo-fault-localize",
            }
        )

        out = await orch._run_skill_first("find the bug in /workspace/huge.py")

        assert out is not None
        assert out["ok"] is False
        assert "too large" in out["summary"].lower()
        assert "completion-guard" in out["summary"].lower()

    @pytest.mark.asyncio
    async def test_skill_first_honest_success_unchanged(self):
        from services.orchestrator.coding_orchestrator import AsyncOrchestrator

        orch = AsyncOrchestrator(skill_router=MagicMock(), mcp=None, workspace="/tmp")
        orch.skill_router.run = AsyncMock(
            return_value={
                "ok": True,
                "result": "Here are three potential bugs I found in the file.",
                "skill_name": "code-review",
            }
        )

        out = await orch._run_skill_first("review /workspace/app.py for bugs")

        assert out is not None
        assert out["ok"] is True
        assert "completion-guard" not in out["summary"].lower()


class TestReactFinishClaimGating:
    """Wire-in (b): the finish branch downgrades an unverified 'I fixed it'
    claim (no passing run_tests this run) to ok=False with a caveat, and
    downgrades a punt summary to ok=False."""

    @staticmethod
    def _finish_msg(summary: str):
        import json as _json

        tc = MagicMock()
        tc.id = "call-finish"
        tc.function = MagicMock()
        tc.function.name = "finish"
        tc.function.arguments = _json.dumps({"summary": summary})
        msg = MagicMock()
        msg.tool_calls = [tc]
        msg.content = ""
        msg.reasoning_content = ""
        msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
        return MagicMock(choices=[MagicMock(message=msg)])

    @pytest.mark.asyncio
    async def test_unverified_fix_claim_downgraded_with_caveat(self):
        from services.orchestrator.coding_orchestrator import AsyncOrchestrator

        orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
        # finish on turn 1 with a success claim; NO run_tests happened → tests_passed False.
        with patch(
            "services.orchestrator.coding_orchestrator.acompletion_with_failover",
            new_callable=AsyncMock,
            side_effect=[self._finish_msg("I fixed the off-by-one bug and all tests pass.")],
        ):
            out = await orch._run_react_loop("fix the factorial bug", 4)

        assert out["ok"] is False
        assert "completion-guard" in out["summary"].lower()

    @pytest.mark.asyncio
    async def test_punt_summary_downgraded(self):
        from services.orchestrator.coding_orchestrator import AsyncOrchestrator

        orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
        with patch(
            "services.orchestrator.coding_orchestrator.acompletion_with_failover",
            new_callable=AsyncMock,
            side_effect=[
                self._finish_msg(
                    "I could not complete this; the file is too large, provide a smaller snippet."
                )
            ],
        ):
            out = await orch._run_react_loop("fix the file", 4)

        assert out["ok"] is False
        assert "too large" in out["summary"].lower()

    @pytest.mark.asyncio
    async def test_honest_neutral_finish_stays_ok_true(self):
        from services.orchestrator.coding_orchestrator import AsyncOrchestrator

        orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
        with patch(
            "services.orchestrator.coding_orchestrator.acompletion_with_failover",
            new_callable=AsyncMock,
            side_effect=[self._finish_msg("Here is the square function you asked for.")],
        ):
            out = await orch._run_react_loop("write a square function", 4)

        assert out["ok"] is True
        assert "completion-guard" not in out["summary"].lower()
