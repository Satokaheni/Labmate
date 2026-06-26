"""Step definitions for the message-sequence-repair BDD feature.

Follows the existing *_bdd.py idiom: patch
``services.orchestrator.coding_orchestrator.acompletion_with_failover`` with a
scripted side_effect, and assert on the messages the mock received.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.coding_orchestrator import AsyncOrchestrator
from services.orchestrator.message_repair import sanitize_messages, validate_messages
from tests.conftest import run_async

scenarios("features/message_sequence_repair.feature")


# ── helpers ──────────────────────────────────────────────────────────────────

def _assistant_with_calls(*ids):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": i, "type": "function",
             "function": {"name": "run_bash", "arguments": "{}"}}
            for i in ids
        ],
    }


def _tool_call_resp(name, args):
    tc = MagicMock()
    tc.id = f"call-{name}"
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = json.dumps(args)
    msg = MagicMock()
    msg.tool_calls = [tc]
    msg.content = ""
    msg.reasoning_content = ""
    msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    return MagicMock(choices=[MagicMock(message=msg)])


def _finish_resp(summary="done"):
    m = MagicMock(tool_calls=None, content=summary)
    m.model_dump = lambda: {"role": "assistant", "content": summary}
    return MagicMock(choices=[MagicMock(message=m)])


@pytest.fixture
def ctx():
    return {"messages": None, "out": None, "captured": [], "result": None}


# ── pure-sanitizer scenarios ─────────────────────────────────────────────────

@given("a message list with an orphaned tool result")
def _orphan(ctx):
    ctx["messages"] = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "go"},
        {"role": "tool", "tool_call_id": "ghost", "content": "stale"},
    ]


@given("a well-formed edit-tool-finish message list")
def _wellformed(ctx):
    ctx["messages"] = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "edit the file"},
        _assistant_with_calls("c1"),
        {"role": "tool", "tool_call_id": "c1", "content": "wrote file"},
        {"role": "assistant", "content": "done"},
    ]


@given("a message list with a synthetic user turn injected after a tool result")
def _synthetic(ctx):
    ctx["messages"] = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "go"},
        _assistant_with_calls("c1"),
        {"role": "tool", "tool_call_id": "c1", "content": "tool output"},
        {"role": "user", "content": "[steering] focus on tests"},
    ]


@when("the messages are sanitized")
def _sanitize(ctx):
    ctx["out"] = sanitize_messages(ctx["messages"])


@then("the orphaned tool result is gone")
def _orphan_gone(ctx):
    assert all(
        m.get("tool_call_id") != "ghost" for m in ctx["out"]
    )
    assert not any(m.get("role") == "tool" for m in ctx["out"])


@then("the system and user prefix are unchanged")
def _prefix_unchanged(ctx):
    assert ctx["out"][0] == {"role": "system", "content": "S"}
    assert ctx["out"][1] == {"role": "user", "content": "go"}


@then("the sanitized list validates clean")
def _validates_clean(ctx):
    assert validate_messages(ctx["out"]) == []


@then("the sanitized list is identical to the input")
def _identical(ctx):
    assert ctx["out"] == ctx["messages"]


# ── loop-boundary scenario ───────────────────────────────────────────────────

@given("an AsyncOrchestrator with no skill router and a stub mcp")
def _orch(ctx):
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    mcp = AsyncMock()
    res = MagicMock()
    res.content = [MagicMock(text="output")]
    res.isError = False
    mcp.call_tool.return_value = res
    orch.mcp = mcp
    ctx["orch"] = orch


@given("the model calls run_bash on turn 1 then finish on turn 2")
def _script(ctx):
    ctx["responses"] = [
        _tool_call_resp("run_bash", {"command": "echo hi"}),
        _finish_resp("done"),
    ]


@when(parsers.parse('the react loop runs the goal "{goal}"'))
def _run_loop(ctx, goal):
    captured = ctx["captured"]
    responses = ctx["responses"]
    state = {"i": 0}

    async def fake_failover(*args, **kwargs):
        captured.append([dict(m) for m in kwargs["messages"]])
        i = state["i"]
        state["i"] += 1
        return responses[i]

    with patch(
        "services.orchestrator.coding_orchestrator.acompletion_with_failover",
        new=fake_failover,
    ):
        ctx["result"] = run_async(ctx["orch"]._run_react_loop(goal, 4))


@then("every message list the model received validates clean")
def _all_clean(ctx):
    assert ctx["captured"], "model was never called"
    for msgs in ctx["captured"]:
        assert validate_messages(msgs) == [], f"malformed: {msgs}"


@then("the result ok is True")
def _ok(ctx):
    assert ctx["result"]["ok"] is True
