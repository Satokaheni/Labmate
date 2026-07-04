# tests/services/orchestrator/test_verification_stop_bdd.py
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

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
def _orch(ctx, tmp_path):
    # Set up skill_router to return a code-sandbox run_tests envelope (PASSING).
    skill_router = AsyncMock()
    test_result = {
        "passed": 1,
        "failed": 0,
        "errors": 0,
        "output": "1 passed",
        "timed_out": False,
    }
    envelope = {
        "ok": True,
        "result": {
            "content": [{"type": "text", "text": json.dumps(test_result)}],
            "isError": False,
        },
    }
    skill_router.execute.return_value = envelope

    orch = AsyncOrchestrator(skill_router=skill_router, mcp=None, workspace=str(tmp_path))
    # write_file/read_file now execute directly (execute_local_tool) against a
    # real tmp_path workspace; the read-back verify reads the real file it
    # just wrote, so no local-tool mock is needed. local_client stays truthy
    # so the LOCAL_TOOL_NAMES dispatch branch is taken.
    orch.local_client = MagicMock()
    ctx["orch"] = orch


# ── Given steps ──────────────────────────────────────────────────────────────


@given(parsers.parse('MAX_VERIFY_NUDGES is "{value}"'))
def _set_max(ctx, value, monkeypatch):
    monkeypatch.setenv("MAX_VERIFY_NUDGES", value)


@given(parsers.parse('the model writes file "{path}" on turn {turn:d}'))
def _write_on_turn(ctx, path, turn):
    _ensure_len(ctx, turn)
    ctx["responses"][turn - 1] = _tool_call_msg("write_file", {"path": path, "content": "x = 1"})


@given(parsers.parse('the model calls finish with summary "{summary}" on turn {turn:d}'))
def _finish_on_turn(ctx, summary, turn):
    _ensure_len(ctx, turn)
    ctx["responses"][turn - 1] = _tool_call_msg("finish", {"summary": summary})


@given(parsers.parse('the model calls finish with summary "{summary}" on turn 1'))
def _finish_turn1(ctx, summary):
    _ensure_len(ctx, 1)
    ctx["responses"][0] = _tool_call_msg("finish", {"summary": summary})


@given(parsers.parse("the model calls run_tests on turn {turn:d} with a passing result"))
def _run_tests_on_turn(ctx, turn):
    _ensure_len(ctx, turn)
    ctx["responses"][turn - 1] = _tool_call_msg("run_tests", {"path": "tests/"})


# ── When step ────────────────────────────────────────────────────────────────


@when(parsers.parse('the verification-stop loop runs the goal "{goal}"'))
def _run(ctx, goal):
    # Count nudges by intercepting the emitted verify.nudge event.
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
            with patch(
                "services.orchestrator.coding_orchestrator.litellm.acompletion",
                new_callable=AsyncMock,
                side_effect=ctx["responses"],
            ) as mock_compl:
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


@then(parsers.parse("a verification nudge was injected exactly {n:d} time"))
@then(parsers.parse("a verification nudge was injected exactly {n:d} times"))
def _nudge_count(ctx, n):
    assert ctx["nudges"] == n


@then(parsers.parse("the model was called exactly {n:d} times"))
def _call_count(ctx, n):
    assert ctx["mock"].call_count == n
