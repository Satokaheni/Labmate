# tests/services/orchestrator/test_infra_aware_verification_bdd.py
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, scenarios, then, when

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
def _orch_broken_toolchain(ctx, tmp_path):
    # run_tests now runs as a direct local subprocess. Simulate a broken
    # toolchain the way a real shell would report it: the subprocess runs
    # (does not raise) but exits non-zero with output classify_test_attempt
    # recognizes as an infra marker ("command not found" -> pytest/test
    # runner missing), so the SAME accounting path used by a real run
    # (shape_run_tests_result -> classify_test_attempt) marks it infra_error,
    # matching the pod-era "run_tests tool not available" scenario.
    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"/bin/sh: pytest: command not found", None))
    fake_proc.returncode = 127
    fake_proc.kill = MagicMock()
    fake_proc.wait = AsyncMock()
    ctx["fake_proc"] = fake_proc

    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace=str(tmp_path))
    # write_file/read_file now execute directly (execute_local_tool) against a
    # real tmp_path workspace; the read-back verify reads the real file it
    # just wrote, so no local-tool mock is needed. local_client stays truthy
    # so the LOCAL_TOOL_NAMES dispatch branch is taken.
    orch.local_client = MagicMock()
    ctx["orch"] = orch


# ── Given steps ──────────────────────────────────────────────────────────────


@given("the agent edits a file and then calls run_tests twice")
def _edit_and_test_twice(ctx):
    # Turn 1: write_file
    ctx["responses"].append(
        _tool_call_msg("write_file", {"path": "src/app.py", "content": "x = 1"})
    )
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

    captured = []

    class _Emitter:
        async def emit(self, type, **f):
            captured.append(type)

    # write_file/read_file now execute directly against the real tmp_path
    # workspace (execute_local_tool), so the write-then-read-back verify uses
    # the actual file content on disk — no local-tool mock needed.
    async def run_with_capture():
        token = _events.current_emitter.set(_Emitter())
        try:
            with (
                patch(
                    "services.orchestrator.coding_orchestrator.litellm.acompletion",
                    new_callable=AsyncMock,
                    side_effect=ctx["responses"],
                ) as mock_compl,
                patch(
                    "services.orchestrator.coding_orchestrator.asyncio.create_subprocess_shell",
                    new=AsyncMock(return_value=ctx["fake_proc"]),
                ),
            ):
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
    assert (
        "UNVERIFIED" in summary
    ), f"Expected 'UNVERIFIED' in summary, got: {ctx['result']['summary']}"


@then("the final summary does not claim the tests passed")
def _no_pass_claim(ctx):
    summary = ctx["result"]["summary"].lower()
    assert (
        "all tests pass" not in summary
    ), f"Summary should not claim tests passed: {ctx['result']['summary']}"


@then("the unverified note contains the specific infra reason")
def _infra_reason_captured(ctx):
    summary = ctx["result"]["summary"]
    # The reason should be "test toolchain error: command not found" (the
    # infra marker classify_test_attempt matched in the fake subprocess output).
    assert (
        "command not found" in summary.lower()
    ), f"Expected specific infra reason 'command not found' in summary, got: {summary}"
