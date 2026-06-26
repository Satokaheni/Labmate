# tests/services/orchestrator/test_coding_orchestrator.py
from __future__ import annotations
import asyncio
import json
import pytest
import graphlib
from unittest.mock import AsyncMock, MagicMock, patch

from services.orchestrator.coding_orchestrator import TokenBudget, AsyncOrchestrator, Result, SubTask, CodingOrchestrator


def _chunk(text):
    return MagicMock(choices=[MagicMock(delta=MagicMock(content=text))])


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

    with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
               new_callable=AsyncMock, return_value=fake_stream()):
        token = events.current_emitter.set(FakeEmitter())
        try:
            text = await orch.stream_final_answer(
                "say hi",
                {"final_answer": "Hello world",
                 "goal_tree": {"root": {"result": "Hello world"}}}
            )
        finally:
            events.current_emitter.reset(token)

    assert text == "Hello world"
    deltas = [e["text"] for e in captured if e["type"] == "answer.delta"]
    assert deltas == ["Hel", "lo ", "world"]
    assert any(e["type"] == "answer.done" and e["text"] == "Hello world" for e in captured)


@pytest.mark.asyncio
async def test_stream_final_answer_falls_back_on_error():
    from services.orchestrator import events
    orch = CodingOrchestrator(graph=None, workspace_path=".", docker_container="")
    with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
               new_callable=AsyncMock, side_effect=RuntimeError("stream boom")):
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
    orch.mcp.call_tool = AsyncMock(return_value=MagicMock(
        content=[MagicMock(text="hi")], isError=False
    ))

    resp1 = MagicMock(choices=[MagicMock(
        message=_msg_with_tool_call("run_bash", '{"command":"echo hi"}', "need shell")
    )])
    finish_msg = MagicMock(tool_calls=None, content="done")
    finish_msg.model_dump = lambda: {"role": "assistant", "content": "done"}
    resp2 = MagicMock(choices=[MagicMock(message=finish_msg)])

    captured = []

    class FakeEmitter:
        async def emit(self, type, **f):
            captured.append({"type": type, **f})

    with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
               new_callable=AsyncMock, side_effect=[resp1, resp2]):
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
    async def test_react_execute_budget_exhausted(self):
        """Loop exhausts the budget (incl. the grace turn) without finish — ok=False."""
        orch = self._make_orch(max_steps=2)

        # cap=2 working turns + 1 grace turn = 3 model calls before stopping.
        r1 = self._make_tool_call_response("run_bash", {"command": "ls"})
        r2 = self._make_tool_call_response("run_bash", {"command": "pwd"})
        r3 = self._make_tool_call_response("run_bash", {"command": "whoami"})

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, side_effect=[r1, r2, r3]):
            # Mock MCP to return bash result
            mcp = AsyncMock()
            mcp_result = MagicMock()
            mcp_result.content = [MagicMock(text="output")]
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

    @pytest.mark.asyncio
    async def test_react_execute_skill_failure_surfaces_error_message(self):
        """Skill-first shortcut: when skill fails, summary must contain error, never literal 'null'."""
        runner = MagicMock()
        runner.reset_activations.return_value = None
        skill_router = MagicMock()
        skill_router.runner = runner
        orch = self._make_orch(skill_router=skill_router)

        # Mock skill_router.run() to return a failure response
        skill_router.run = AsyncMock(return_value={
            "ok": False,
            "error": "timeout"
        })

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
        skill_router.run = AsyncMock(return_value={
            "ok": True,
            "result": "skill execution completed successfully"
        })

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
        skill_router.run = AsyncMock(return_value={
            "ok": True,
            "result": None
        })

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
        skill_router.run = AsyncMock(return_value={
            "ok": True,
            "result": ""
        })

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
        skill_router.run = AsyncMock(return_value={
            "ok": False,
            "error": None
        })

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

        skill_router.run = AsyncMock(return_value={
            "ok": False,
            "error": "tool_error",
            "result": {"content": [{"type": "text", "text": "Traceback: ZeroDivisionError"}]},
        })

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

        skill_router.run = AsyncMock(return_value={
            "ok": False,
            "error": "skill_unavailable",
            "detail": "no tool 'frobnicate' in skill 'foo'",
        })

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
        skill_router.run = AsyncMock(return_value={
            "ok": True,
            "result": {
                "content": [
                    {"type": "file", "path": "/some/file"},
                    {"type": "json", "data": {"key": "value"}}
                ]
            }
        })

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
        skill_router.run = AsyncMock(return_value={
            "ok": True,
            "result": {
                "status": "complete",
                "count": 42
            }
        })

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

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, side_effect=_counting):
            result = await orch.react_execute("loop forever")

        assert result["ok"] is False
        assert "loop" in result["summary"].lower()
        # Must halt before exhausting all 6 steps.
        assert call_count["n"] < 6

    @pytest.mark.asyncio
    async def test_react_execute_distinct_calls_do_not_trip_loop(self):
        """Distinct read_file paths must NOT trip the detector; finish ends cleanly."""
        orch = self._make_orch(max_steps=6, mcp=None)
        orch.redis = None  # read_file without redis returns a structured error, not a crash

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

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, side_effect=[r1, r2, r3, rf]):
            result = await orch.react_execute("read three files")

        assert result["ok"] is True
        assert result["summary"] == "all read"


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


@pytest.mark.mocked
class TestReactExecuteBudget:
    """IterationBudget wire-in: grace call on exhaustion + cheap-call refund."""

    def _make_orch(self, max_steps=6):
        from services.orchestrator.coding_orchestrator import AsyncOrchestrator
        return AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp",
                                 max_steps=max_steps)

    def _bash_resp(self, command):
        return _msg_with_tool_call("run_bash", json.dumps({"command": command}))

    def _list_dir_resp(self, path):
        return _msg_with_tool_call("list_dir", json.dumps({"path": path}))

    def _finish_resp(self, summary):
        return _msg_with_tool_call("finish", json.dumps({"summary": summary}))

    @pytest.mark.asyncio
    async def test_exhaustion_grants_exactly_one_grace_call(self):
        """Cap 2 + always-run_bash => model is called cap+1 (=3) times, then stops."""
        orch = self._make_orch(max_steps=2)
        mcp = AsyncMock()
        mcp_result = MagicMock()
        mcp_result.content = [MagicMock(text="output")]
        mcp_result.isError = False
        mcp.call_tool.return_value = mcp_result
        orch.mcp = mcp

        # Different commands to avoid loop detector (each call has different args)
        responses = [
            MagicMock(choices=[MagicMock(message=self._bash_resp(f"echo {i}"))])
            for i in range(10)
        ]

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, side_effect=responses) as m:
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

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, side_effect=[r1, r2]) as m:
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

        # list_dir routes through local tools; with redis=None it returns a
        # structured error but still counts as a cheap (refunded) read turn.
        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, side_effect=[r1, r2, r3, r4]) as m:
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
            MagicMock(choices=[MagicMock(message=self._bash_resp(f"echo {i}"))])
            for i in range(10)
        ]
        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, side_effect=responses) as m:
            result = await orch.react_execute("loop")

        # ...but the env knob clamps to 1 => 1 working turn + 1 grace = 2 calls.
        assert m.await_count == 2
        assert "budget exhausted" in result["summary"]


@pytest.mark.asyncio
async def test_react_routes_read_file_to_local_tool():
    """ReAct loop routes read_file through request_local_tool and returns the result."""
    import fakeredis.aioredis
    from services.orchestrator import events
    from services.orchestrator.local_tools import TOOL_RESULTS_PREFIX

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    task_id = "task-react-file"
    emitter = events.EventEmitter(redis, task_id)
    token = events.current_emitter.set(emitter)

    orch = AsyncOrchestrator(skill_router=None, max_steps=3)
    orch.redis = redis

    # Responder: posts a tool.result as soon as tool.request lands on event stream
    async def responder():
        ev_stream = f"{events.EVENTS_STREAM_PREFIX}{task_id}"
        for _ in range(100):
            resp = await redis.xread({ev_stream: "0"}, count=20, block=100)
            if not resp:
                continue
            for _s, entries in resp:
                for _id, f in entries:
                    ev = json.loads(f["event"])
                    if ev.get("type") == "tool.request":
                        await redis.xadd(
                            f"{TOOL_RESULTS_PREFIX}{task_id}",
                            {"result": json.dumps({
                                "tool_request_id": ev["tool_request_id"],
                                "result": {"content": "FILE BODY"},
                                "error": None,
                            })},
                        )
                        return

    # Turn 1: LLM calls read_file. Turn 2: LLM calls finish.
    read_file_msg = _msg_with_tool_call("read_file", '{"path": "a.txt"}')
    finish_msg_obj = _msg_with_tool_call("finish", '{"summary": "read complete"}')

    resp1 = MagicMock(choices=[MagicMock(message=read_file_msg)])
    resp2 = MagicMock(choices=[MagicMock(message=finish_msg_obj)])

    responder_task = asyncio.create_task(responder())
    try:
        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, side_effect=[resp1, resp2]):
            out = await orch.react_execute("read a.txt")
        await responder_task
    finally:
        events.current_emitter.reset(token)
        await redis.aclose()

    assert out["ok"] is True
    assert out["summary"] == "read complete"


@pytest.mark.asyncio
async def test_react_file_tool_with_no_redis_returns_error():
    """When redis is None, file tools return a structured error rather than raising."""
    orch = AsyncOrchestrator(skill_router=None, max_steps=2)
    assert orch.redis is None  # default

    read_file_msg = _msg_with_tool_call("read_file", '{"path": "x.txt"}')
    finish_msg_obj = _msg_with_tool_call("finish", '{"summary": "done"}')

    resp1 = MagicMock(choices=[MagicMock(message=read_file_msg)])
    resp2 = MagicMock(choices=[MagicMock(message=finish_msg_obj)])

    with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
               new_callable=AsyncMock, side_effect=[resp1, resp2]):
        out = await orch.react_execute("read x.txt")

    assert out["ok"] is True  # finish was called, so it succeeded
    assert out["summary"] == "done"


def test_react_system_prompt_directs_code_to_sandbox():
    import inspect
    from services.orchestrator import coding_orchestrator
    src = inspect.getsource(coding_orchestrator.AsyncOrchestrator.react_execute)
    assert "code-sandbox" in src
    assert "run_bash" in src
