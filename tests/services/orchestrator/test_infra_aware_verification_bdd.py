# tests/services/orchestrator/test_infra_aware_verification_bdd.py
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.coding_orchestrator import AsyncOrchestrator
from tests.conftest import run_async

scenarios("features/infra_aware_verification.feature")


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


@pytest.fixture
def ctx():
    return {"responses": [], "result": None, "mock": None}


# ── Background ───────────────────────────────────────────────────────────────

@given("a ReAct orchestrator with a broken test toolchain")
def _orch_broken_toolchain(ctx):
    # skill_router that returns an infra error (no tool available)
    # The error will be shaped by shape_sandbox_test_result into a response
    # that classify_test_attempt will mark as infra_error
    skill_router = AsyncMock()
    error_envelope = {
        "ok": False,
        "error": "skill_unavailable",
        "detail": "run_tests tool not available",
    }
    skill_router.execute.return_value = error_envelope

    orch = AsyncOrchestrator(skill_router=skill_router, mcp=None, workspace="/tmp")
    # write_file flows through request_local_tool -> stub redis truthy.
    orch.redis = MagicMock()
    ctx["orch"] = orch


# ── Given steps ──────────────────────────────────────────────────────────────

@given("the agent edits a file and then calls run_tests twice")
def _edit_and_test_twice(ctx):
    # Turn 1: write_file
    ctx["responses"].append(_tool_call_msg("write_file", {"path": "src/app.py", "content": "x = 1"}))
    # Turn 2: run_tests (will get infra error)
    ctx["responses"].append(_tool_call_msg("run_tests", {"path": "tests/"}))
    # Turn 3: run_tests again with different args to avoid loop detection (still infra error)
    ctx["responses"].append(_tool_call_msg("run_tests", {"path": "tests/", "timeout": 60000}))
    # Turn 4: finish
    ctx["responses"].append(_tool_call_msg("finish", {"summary": "I fixed the bug"}))


# ── When step ────────────────────────────────────────────────────────────────

@when("the agent attempts to finish")
def _run(ctx):
    # Create a real EventEmitter that captures events instead of writing to Redis
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
                    result = await ctx["orch"].react_execute("fix the bug")
                    ctx["mock"] = mock_compl
                    return result
        finally:
            _events.current_emitter.reset(token)

    ctx["result"] = run_async(run_with_capture())


# ── Then steps ───────────────────────────────────────────────────────────────

@then("the final summary marks the result as unverified")
def _summary_unverified(ctx):
    summary = ctx["result"]["summary"].upper()
    assert "UNVERIFIED" in summary, f"Expected 'UNVERIFIED' in summary, got: {ctx['result']['summary']}"


@then("the final summary does not claim the tests passed")
def _no_pass_claim(ctx):
    summary = ctx["result"]["summary"].lower()
    assert "all tests pass" not in summary, f"Summary should not claim tests passed: {ctx['result']['summary']}"
