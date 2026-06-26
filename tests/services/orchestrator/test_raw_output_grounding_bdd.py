from __future__ import annotations

import json
import re
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.tool_grounding import ground_tool_result
from tests.conftest import run_async

pytestmark = [pytest.mark.bdd, pytest.mark.mocked]

scenarios("features/raw_output_grounding.feature")


@pytest.fixture
def ctx():
    return {
        "text": "",
        "budget": 16000,
        "grounded": "",
        "orch": None,
        "model_calls": [],
        "react_result": None,
    }


# ── Pure-helper scenarios ──────────────────────────────────────────────────
@given(parsers.parse("a tool output of {n:d} characters"))
def _output_n_chars(ctx, n):
    ctx["text"] = "x" * n


@given("a tool output that is a long passing-test preamble followed by a FAILED assertion at the very end")
def _failing_test_output(ctx):
    preamble = "PASSED test_a\n" * 4000  # ~56k chars of noise
    tail = (
        "FAILED tests/test_math.py::test_add - assert 5 == 4\n"
        "E       assert 5 == 4\n"
    )
    ctx["text"] = preamble + tail


@given(parsers.parse("a tool-result budget of {budget:d} characters"))
def _budget(ctx, budget):
    ctx["budget"] = budget


@when("the output is grounded")
def _ground(ctx):
    ctx["grounded"] = ground_tool_result(ctx["text"], ctx["budget"])


@then("the grounded text equals the original output exactly")
def _equals_original(ctx):
    assert ctx["grounded"] == ctx["text"]


@then("the grounded text contains no truncation marker")
def _no_marker(ctx):
    assert "truncated" not in ctx["grounded"]


@then("the grounded text is no longer than the budget plus the marker")
def _within_budget_plus_marker(ctx):
    m = re.search(r"\n…\[\d+ chars truncated\]…\n", ctx["grounded"])
    marker_len = len(m.group(0)) if m else 0
    assert len(ctx["grounded"]) <= ctx["budget"] + marker_len


@then("the grounded text starts with the head of the original output")
def _starts_with_head(ctx):
    assert ctx["grounded"][:10] == ctx["text"][:10]


@then("the grounded text ends with the tail of the original output")
def _ends_with_tail(ctx):
    assert ctx["grounded"][-10:] == ctx["text"][-10:]


@then("the grounded text contains a truncation marker reporting the dropped char count")
def _marker_with_count(ctx):
    assert re.search(r"\n…\[\d+ chars truncated\]…\n", ctx["grounded"]) is not None


@then("the grounded text contains the FAILED assertion line")
def _has_failed_line(ctx):
    assert "FAILED tests/test_math.py::test_add" in ctx["grounded"]


@then("the grounded text contains the assert detail line")
def _has_assert_detail(ctx):
    assert "assert 5 == 4" in ctx["grounded"]


# ── Wired-loop scenarios ───────────────────────────────────────────────────
def _bash_then_finish_responses():
    """resp1 = call run_bash; resp2 = finish with plain content."""
    tc = MagicMock()
    tc.id = "call_bash"
    tc.function = MagicMock()
    tc.function.name = "run_bash"
    tc.function.arguments = json.dumps({"command": "echo x"})
    msg1 = MagicMock()
    msg1.content = None
    msg1.tool_calls = [tc]
    msg1.reasoning_content = ""
    msg1.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    resp1 = MagicMock(choices=[MagicMock(message=msg1)])

    finish_msg = MagicMock(tool_calls=None, content="done")
    finish_msg.model_dump = lambda: {"role": "assistant", "content": "done"}
    resp2 = MagicMock(choices=[MagicMock(message=finish_msg)])
    return resp1, resp2


@given("a ReAct orchestrator wired to a fake model that runs one bash command then finishes")
def _orch_bash_finish(ctx):
    from services.orchestrator.coding_orchestrator import AsyncOrchestrator

    orch = AsyncOrchestrator(skill_router=None, mcp=AsyncMock(), workspace="/tmp", max_steps=6)
    ctx["orch"] = orch


@given(parsers.parse('the bash command returns "{text}" as its only output'))
def _bash_returns_text(ctx, text):
    res = MagicMock()
    res.content = [MagicMock(text=text)]
    res.isError = False
    ctx["orch"].mcp.call_tool = AsyncMock(return_value=res)


@given(parsers.parse('the bash command returns {n:d} characters ending in "{sentinel}"'))
def _bash_returns_huge(ctx, n, sentinel):
    body = ("A" * n) + sentinel
    res = MagicMock()
    res.content = [MagicMock(text=body)]
    res.isError = False
    ctx["orch"].mcp.call_tool = AsyncMock(return_value=res)


@when(parsers.parse('the goal "{goal}" is executed'))
def _execute(ctx, goal):
    orch = ctx["orch"]
    resp1, resp2 = _bash_then_finish_responses()
    scripted = [resp1, resp2]

    async def _spy(*a, **k):
        # Snapshot the messages list (copy each dict) so the post-tool-call
        # snapshot includes the appended {"role":"tool",...} entry.
        ctx["model_calls"].append([dict(m) for m in k["messages"]])
        return scripted[min(len(ctx["model_calls"]) - 1, len(scripted) - 1)]

    async def _run():
        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, side_effect=_spy):
            return await orch.react_execute(goal)

    ctx["react_result"] = run_async(_run())


def _last_tool_message_content(ctx) -> str:
    # The 2nd model call carries the appended tool result.
    assert len(ctx["model_calls"]) >= 2, "expected at least two model calls"
    msgs = ctx["model_calls"][1]
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert tool_msgs, "no tool message appended to context"
    return tool_msgs[-1]["content"]


@then(parsers.parse('the tool message appended to the model context contains "{needle}" verbatim'))
def _tool_msg_contains_verbatim(ctx, needle):
    assert needle in _last_tool_message_content(ctx)


@then("the tool message contains no truncation marker")
def _tool_msg_no_marker(ctx):
    assert "truncated" not in _last_tool_message_content(ctx)


@then("the tool message appended to the model context contains a truncation marker")
def _tool_msg_has_marker(ctx):
    assert "truncated" in _last_tool_message_content(ctx)


@then(parsers.parse('the tool message appended to the model context contains "{needle}"'))
def _tool_msg_contains(ctx, needle):
    assert needle in _last_tool_message_content(ctx)
