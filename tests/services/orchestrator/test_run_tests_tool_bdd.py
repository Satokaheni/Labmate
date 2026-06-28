"""Step definitions for the run_tests tool + reliable write_file BDD feature."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.coding_orchestrator import AsyncOrchestrator
from services.orchestrator.prompt_assembler import PromptAssembler
from tests.conftest import run_async

scenarios("features/run_tests_tool.feature")


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
    return {
        "responses": [],
        "result": None,
        "tool_results": [],   # captured tool-message contents (role == "tool")
        "assembler": None,
    }


# ── Background ───────────────────────────────────────────────────────────────

@given("an AsyncOrchestrator with no skill router and no mcp")
def _orch_no_mcp(ctx):
    ctx["orch"] = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")


@given("an AsyncOrchestrator with no skill router and a stub bash seam")
def _orch_stub_bash(ctx):
    # For run_tests, we need a mock skill_router since the code-sandbox skill
    # dispatch path requires it (the old exec_run bash-seam path is gone).
    skill_router = AsyncMock()
    orch = AsyncOrchestrator(skill_router=skill_router, mcp=None, workspace="/tmp")
    ctx["orch"] = orch
    ctx["skill_router"] = skill_router


@given("an AsyncOrchestrator with no skill router and a local tool client")
def _orch_local_client(ctx):
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    orch.redis = MagicMock()  # presence triggers the LOCAL_TOOL_NAMES branch
    ctx["orch"] = orch


# ── Given: prompt assembler / bash seam / local client programming ───────────

@given("the prompt assembler builds the tool list")
def _build_tools(ctx):
    ctx["assembler"] = PromptAssembler(skill_router=None, codegraph_enabled=False)


@given(parsers.parse('the bash seam returns exit code {code:d} with output "{output}"'))
def _bash_returns(ctx, code, output):
    # Set up skill_router to return a code-sandbox run_tests envelope.
    # The skill_router.execute() method returns {ok, result, error, detail}.
    # The shape_sandbox_test_result helper parses this into {ok, exit_code, raw_output}.
    raw_output = output.replace("\\n", "\n")
    test_result = {
        "passed": 0 if code != 0 else 1,
        "failed": 0 if code == 0 else 1,
        "errors": 0,
        "output": raw_output,
        "timed_out": False,
    }
    envelope = {
        "ok": True,
        "result": {
            "content": [{"type": "text", "text": json.dumps(test_result)}],
            "isError": code != 0,
        },
    }
    if "skill_router" in ctx:
        ctx["skill_router"].execute.return_value = envelope


@given(parsers.parse('the write_file client reports success but the file reads back as "{readback}"'))
def _client_mismatch(ctx, readback):
    ctx["readback"] = readback
    ctx["write_ok"] = True


@given(parsers.parse('the write_file client reports success and the file reads back as "{readback}"'))
def _client_match(ctx, readback):
    ctx["readback"] = readback
    ctx["write_ok"] = True


# ── Given: scripted model turns ──────────────────────────────────────────────

def _ensure_len(ctx, turn):
    while len(ctx["responses"]) < turn:
        ctx["responses"].append(_tool_call_msg("finish", {"summary": "filler"}))


@given(parsers.parse('the model calls run_tests with path "{path}" on turn {turn:d}'))
def _run_tests_turn(ctx, path, turn):
    _ensure_len(ctx, turn)
    ctx["responses"][turn - 1] = _tool_call_msg("run_tests", {"path": path})


@given(parsers.parse('the model calls write_file with path "{path}" and content "{content}" on turn {turn:d}'))
def _write_file_turn(ctx, path, content, turn):
    _ensure_len(ctx, turn)
    ctx["responses"][turn - 1] = _tool_call_msg(
        "write_file", {"path": path, "content": content}
    )


@given(parsers.parse('the model calls finish with summary "{summary}" on turn {turn:d}'))
def _finish_turn(ctx, summary, turn):
    _ensure_len(ctx, turn)
    ctx["responses"][turn - 1] = _tool_call_msg("finish", {"summary": summary})


# ── When ─────────────────────────────────────────────────────────────────────

@when("the prompt assembler builds the tool list")
def _when_build(ctx):
    ctx["assembler"] = PromptAssembler(skill_router=None, codegraph_enabled=False)


def _capture_messages(orch, ctx):
    """Patch the loop to record tool-role message contents as they are appended."""
    # _run_react_loop appends tool results as {"role": "tool", ..., "content": ...}.
    # We capture by wrapping request_local_tool / mcp; simplest: read from the
    # returned messages is not exposed, so instead we assert via tool_results
    # gathered by a request_local_tool side_effect (write_file path) and by the
    # bash seam (run_tests path). For run_tests we read the result off the model
    # loop by intercepting json content through a patched events.emit("tool.done").


@when(parsers.parse('react_execute runs the goal "{goal}"'))
def _run_goal(ctx, goal):
    orch = ctx["orch"]

    captured: list[str] = []

    # Capture every tool.done result payload — this is the tool `content` string
    # the model would see, emitted verbatim in _run_react_loop's tool.done event.
    async def _emit(event_type, **kw):
        if event_type == "tool.done" and "result" in kw:
            captured.append(kw["result"])

    # Program the local tool client for write_file scenarios:
    #   first call  (write_file) -> success result
    #   second call (read_file)  -> the programmed read-back
    async def _local(redis, name, args, **kw):
        if name == "read_file":
            return ctx.get("readback")
        return {"ok": True}

    with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
               new_callable=AsyncMock, side_effect=ctx["responses"]), \
         patch("services.orchestrator.coding_orchestrator.events.emit",
               new=_emit), \
         patch("services.orchestrator.coding_orchestrator.request_local_tool",
               new=_local):
        ctx["result"] = run_async(orch.react_execute(goal))

    ctx["tool_results"] = captured


# ── Then: tool list assertions ───────────────────────────────────────────────

@then(parsers.parse('the tool list contains a tool named "{name}"'))
def _tool_list_has(ctx, name):
    names = [t["function"]["name"] for t in ctx["assembler"].tools()]
    assert name in names


@then(parsers.parse('the run_tests tool has a "{param}" parameter'))
def _run_tests_has_param(ctx, param):
    schema = next(t for t in ctx["assembler"].tools() if t["function"]["name"] == "run_tests")
    assert param in schema["function"]["parameters"]["properties"]


# ── Then: run_tests result assertions ────────────────────────────────────────

def _run_tests_payload(ctx) -> dict:
    # The run_tests tool.done result is the json string {ok, exit_code, raw_output}.
    for raw in ctx["tool_results"]:
        try:
            obj = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if "raw_output" in obj:
            return obj
    raise AssertionError(f"no run_tests payload found in {ctx['tool_results']}")


@then(parsers.parse("the tool result json has ok {value}"))
def _result_ok(ctx, value):
    assert _run_tests_payload(ctx)["ok"] is (value == "True")


@then(parsers.parse("the tool result json has exit_code {code:d}"))
def _result_exit(ctx, code):
    assert _run_tests_payload(ctx)["exit_code"] == code


@then(parsers.parse('the tool result raw_output contains "{needle}"'))
def _result_raw_contains(ctx, needle):
    assert needle in _run_tests_payload(ctx)["raw_output"]


# ── Then: write_file verification assertions ─────────────────────────────────

def _write_payload_text(ctx) -> str:
    # Concatenate all tool-result strings; the write_file branch result is among them.
    return "\n".join(ctx["tool_results"])


@then(parsers.parse('the write_file tool result contains "{needle}"'))
def _write_contains(ctx, needle):
    assert needle in _write_payload_text(ctx)


@then(parsers.parse('the write_file tool result does not contain "{needle}"'))
def _write_not_contains(ctx, needle):
    assert needle not in _write_payload_text(ctx)
