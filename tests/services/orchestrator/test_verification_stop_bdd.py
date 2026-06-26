# tests/services/orchestrator/test_verification_stop_bdd.py
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.coding_orchestrator import AsyncOrchestrator
from tests.conftest import run_async

scenarios("features/verification_stop.feature")


# ── helpers ──────────────────────────────────────────────────────────────────

def _tool_call_msg(name: str, arguments: dict):
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


def _ensure_len(ctx, turn):
    while len(ctx["responses"]) < turn:
        ctx["responses"].append(_tool_call_msg("finish", {"summary": "filler"}))


@pytest.fixture
def ctx():
    return {"responses": [], "result": None, "mock": None, "nudges": 0}


# ── Background ───────────────────────────────────────────────────────────────

@given("a verification-stop AsyncOrchestrator with no skill router and no mcp")
def _orch(ctx):
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    # write_file flows through request_local_tool -> stub redis truthy.
    orch.redis = MagicMock()
    # run_tests flows through mcp.call_tool -> stub a PASSING result.
    mcp = AsyncMock()
    mcp_result = MagicMock()
    mcp_result.content = [MagicMock(
        text=json.dumps({"ok": True, "exit_code": 0, "raw_output": "1 passed"})
    )]
    mcp_result.isError = False
    mcp.call_tool.return_value = mcp_result
    orch.mcp = mcp
    ctx["orch"] = orch


# ── Given steps ──────────────────────────────────────────────────────────────

@given(parsers.parse('MAX_VERIFY_NUDGES is "{value}"'))
def _set_max(ctx, value, monkeypatch):
    monkeypatch.setenv("MAX_VERIFY_NUDGES", value)


@given(parsers.parse('the model writes file "{path}" on turn {turn:d}'))
def _write_on_turn(ctx, path, turn):
    _ensure_len(ctx, turn)
    ctx["responses"][turn - 1] = _tool_call_msg(
        "write_file", {"path": path, "content": "x = 1"}
    )


@given(parsers.parse('the model calls finish with summary "{summary}" on turn {turn:d}'))
def _finish_on_turn(ctx, summary, turn):
    _ensure_len(ctx, turn)
    ctx["responses"][turn - 1] = _tool_call_msg("finish", {"summary": summary})


@given(parsers.parse('the model calls finish with summary "{summary}" on turn 1'))
def _finish_turn1(ctx, summary):
    _ensure_len(ctx, 1)
    ctx["responses"][0] = _tool_call_msg("finish", {"summary": summary})


@given(parsers.parse('the model calls run_tests on turn {turn:d} with a passing result'))
def _run_tests_on_turn(ctx, turn):
    _ensure_len(ctx, turn)
    ctx["responses"][turn - 1] = _tool_call_msg("run_tests", {"path": "tests/"})


# ── When step ────────────────────────────────────────────────────────────────

@when(parsers.parse('the verification-stop loop runs the goal "{goal}"'))
def _run(ctx, goal):
    # Count nudges by intercepting the emitted verify.nudge event.
    import services.orchestrator.events as _events
    import services.orchestrator.local_tools

    captured = []

    class _Emitter:
        async def emit(self, type, **f):
            captured.append(type)

    # Mock request_local_tool to return file content for verification
    async def mock_local_tool(redis, name, args, timeout=None):
        if name == "read_file":
            # Return the same content that was written
            return "x = 1"
        return {}

    # Create a real EventEmitter that captures events instead of writing to Redis
    async def run_with_capture():
        token = _events.current_emitter.set(_Emitter())
        try:
            with patch(
                "services.orchestrator.coding_orchestrator.litellm.acompletion",
                new_callable=AsyncMock, side_effect=ctx["responses"],
            ) as mock_compl:
                with patch(
                    "services.orchestrator.coding_orchestrator.request_local_tool",
                    new_callable=AsyncMock, side_effect=mock_local_tool,
                ) as mock_tool:
                    result = await ctx["orch"].react_execute(goal)
                    ctx["mock"] = mock_compl
                    return result
        finally:
            _events.current_emitter.reset(token)

    ctx["result"] = run_async(run_with_capture())
    ctx["nudges"] = captured.count("verify.nudge")


# ── Then steps ───────────────────────────────────────────────────────────────

@then("the result ok is True")
def _ok_true(ctx):
    assert ctx["result"]["ok"] is True


@then(parsers.parse('the result summary contains "{needle}"'))
def _summary_contains(ctx, needle):
    assert needle.lower() in ctx["result"]["summary"].lower()


@then(parsers.parse('a verification nudge was injected exactly {n:d} time'))
@then(parsers.parse('a verification nudge was injected exactly {n:d} times'))
def _nudge_count(ctx, n):
    assert ctx["nudges"] == n


@then(parsers.parse('the model was called exactly {n:d} times'))
def _call_count(ctx, n):
    assert ctx["mock"].call_count == n
