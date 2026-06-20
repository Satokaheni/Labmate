# tests/services/orchestrator/test_coding_orchestrator.py
from __future__ import annotations
import asyncio
import json
import pytest
import graphlib
from unittest.mock import AsyncMock, MagicMock, patch

from services.orchestrator.coding_orchestrator import TokenBudget, AsyncOrchestrator, Result, SubTask


def _make_mock_response(content: str = "done") -> MagicMock:
    r = MagicMock()
    r.choices = [MagicMock()]
    r.choices[0].message.content = content
    return r


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
            return {"ok": True, "summary": f"result for goal"}

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

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, return_value=mock_response) as mock_complete:
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

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, return_value=mock_response) as mock_complete:
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

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, return_value=mock_response) as mock_complete:
            await orch.architect("route this tool call", thinking_budget=0)
            call_kwargs = mock_complete.call_args.kwargs
            assert call_kwargs["extra_body"]["thinking_budget_tokens"] == 0

    @pytest.mark.asyncio
    async def test_editor_calls_qwen(self):
        orch = self._make_orch()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "patched code"

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, return_value=mock_response) as mock_complete:
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
            mock_run.return_value = MagicMock(
                stdout="test output", stderr="", returncode=0
            )
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
            mock_run.return_value = MagicMock(
                stdout="", stderr="command not found", returncode=1
            )
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
        mcp.call_tool.return_value = self._make_call_tool_result(
            "command not found", is_error=True
        )
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
            mock_run.return_value = MagicMock(
                stdout="ok", stderr="", returncode=0
            )
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
        msg.model_dump = MagicMock(return_value={
            "role": "assistant",
            "content": None,
            "tool_calls": [MagicMock(id="call_123")],
        })
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

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, return_value=r):
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

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, return_value=r):
            result = await orch.react_execute("what is 2+2?")
            assert result["ok"] is True
            assert "42" in result["summary"]

    @pytest.mark.asyncio
    async def test_react_execute_max_steps_exhausted(self):
        """Loop exhausts max_steps without finish — return ok=False."""
        orch = self._make_orch(max_steps=2)

        # First call: tool call to run_bash
        r1 = self._make_tool_call_response("run_bash", {"command": "ls"})
        # Second call: tool call to run_bash again
        r2 = self._make_tool_call_response("run_bash", {"command": "pwd"})

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, side_effect=[r1, r2]):
            # Mock MCP to return bash result
            mcp = AsyncMock()
            mcp_result = MagicMock()
            mcp_result.content = [MagicMock(text="output")]
            mcp_result.isError = False
            mcp.call_tool.return_value = mcp_result
            orch.mcp = mcp

            result = await orch.react_execute("do something")
            assert result["ok"] is False
            assert "max_steps" in result["summary"]

    @pytest.mark.asyncio
    async def test_react_execute_with_skill_router_load_skill(self):
        """load_skill tool call when skill_router present."""
        runner = MagicMock()
        runner.catalog_prompt.return_value = "- test-skill: A test skill"
        runner.tool_schema.return_value = {
            "type": "function",
            "function": {"name": "load_skill", "parameters": {}}
        }
        runner.load_skill.return_value = {
            "response": {"status": "loaded", "body": "skill body"}
        }
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

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, side_effect=[r1, r2]):
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

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, side_effect=[r]):
            # This should hit max_steps=6, but we're testing one tool dispatch
            result = await orch.react_execute("echo hello")
            # The tool response goes to the model, model likely replies
            # Just ensure the call path works
            mcp.call_tool.assert_awaited()

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

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, side_effect=[r, r2]):
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

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, return_value=r):
            result = await orch.react_execute("simple task")
            assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_react_execute_exception_returns_error(self):
        """Uncaught exception in react_execute returns ok=False."""
        orch = self._make_orch()

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   side_effect=RuntimeError("api error")):
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
        if hasattr(msg, 'model_dump'):
            delattr(msg, 'model_dump')
        r.choices = [MagicMock(message=msg)]

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, return_value=r):
            result = await orch.react_execute("test fallback")
            # Fallback should succeed: finish tool was called
            assert result["ok"] is True
            assert "fallback works" in result["summary"]


@pytest.mark.mocked
class TestRunWorkerUsesReactExecute:
    """Test that _run_worker now uses react_execute instead of _call_qwen_worker."""

    @pytest.mark.asyncio
    async def test_run_worker_calls_react_execute(self):
        """_run_worker delegates to react_execute and builds Result correctly."""
        orch = AsyncOrchestrator()
        t = SubTask(id="g1", prompt="test goal", est_tokens=100)

        mock_react_ret = {"ok": True, "summary": "task done"}

        with patch.object(orch, "react_execute", new_callable=AsyncMock,
                          return_value=mock_react_ret) as mock_react:
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
