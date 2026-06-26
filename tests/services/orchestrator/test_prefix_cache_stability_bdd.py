from __future__ import annotations
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.coding_orchestrator import AsyncOrchestrator

scenarios("features/prefix_cache_stability.feature")


# ── helpers ────────────────────────────────────────────────────────────────
def _tool_call_msg(name: str, arguments_json: str):
    tc = MagicMock()
    tc.id = "call-" + name
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = arguments_json
    msg = MagicMock()
    msg.tool_calls = [tc]
    msg.content = ""
    msg.reasoning_content = ""
    msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    return MagicMock(choices=[MagicMock(message=msg)])


def _prefix_of(body: dict) -> str:
    """Canonical system+tools prefix string for one recorded request body."""
    system = next(m for m in body["messages"] if m["role"] == "system")
    payload = {"system": system["content"], "tools": body.get("tools", [])}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@pytest.fixture
def captured():
    """Mutable bag the steps share across the scenario."""
    return {"bodies": [], "bodies2": []}


@pytest.fixture
def scripted_completion():
    """
    Patch litellm.acompletion to (a) record the request body it was called with
    and (b) return scripted responses: run_bash on step 1, finish on step 2.
    We capture the body directly here so the BDD suite does not depend on the
    fake_model fixture's internal request-log accessor name.
    """
    responses = [
        _tool_call_msg("run_bash", '{"command":"ls"}'),
        _tool_call_msg("finish", '{"summary":"done"}'),
    ]
    bodies: list[dict] = []

    async def _fake_acompletion(*args, **kwargs):
        # Record a JSON-safe snapshot of the prefix-relevant fields.
        bodies.append({
            "messages": [dict(m) for m in kwargs["messages"]],
            "tools": kwargs.get("tools", []),
        })
        return responses[min(len(bodies) - 1, len(responses) - 1)]

    with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
               new=AsyncMock(side_effect=_fake_acompletion)):
        yield bodies


# ── Background ───────────────────────────────────────────────────────────────
@given("a fake OpenAI-compatible model that records every request body")
def _fake_model_recording(scripted_completion):
    # scripted_completion patches litellm.acompletion and yields the bodies list.
    return scripted_completion


@given("an AsyncOrchestrator with no skill router and no MCP bridge", target_fixture="orch")
def _orch():
    return AsyncOrchestrator(skill_router=None, mcp=None, max_steps=3)


# ── Scenario 1 & 2 shared driver ─────────────────────────────────────────────
@given("the model is scripted to call run_bash on step 1 then finish on step 2")
def _scripted(scripted_completion):
    return scripted_completion


@when(parsers.parse('react_execute runs the goal "{goal}"'), target_fixture="run_bodies")
def _run(orch, scripted_completion, goal):
    import asyncio
    async def _run_async():
        out = await orch.react_execute(goal)
        assert out["ok"] is True
        return scripted_completion          # the recorded bodies list
    return asyncio.run(_run_async())


@then("the model received at least 2 requests")
def _at_least_two(run_bodies):
    assert len(run_bodies) >= 2


@then("the system message of request 2 equals the system message of request 1")
def _system_equal(run_bodies):
    sys1 = next(m for m in run_bodies[0]["messages"] if m["role"] == "system")
    sys2 = next(m for m in run_bodies[1]["messages"] if m["role"] == "system")
    assert sys1 == sys2


@then("the tools list of request 2 equals the tools list of request 1")
def _tools_equal(run_bodies):
    assert run_bodies[0]["tools"] == run_bodies[1]["tools"]


@then("the serialized system+tools prefix of request 2 is byte-identical to request 1")
def _prefix_identical(run_bodies):
    assert _prefix_of(run_bodies[0]) == _prefix_of(run_bodies[1])


# ── Scenario 2: append-only ──────────────────────────────────────────────────
@then("request 2 has strictly more messages than request 1")
def _more_messages(run_bodies):
    assert len(run_bodies[1]["messages"]) > len(run_bodies[0]["messages"])


@then("the messages of request 1 are a prefix of the messages of request 2")
def _messages_are_prefix(run_bodies):
    m1 = run_bodies[0]["messages"]
    m2 = run_bodies[1]["messages"]
    assert m2[: len(m1)] == m1


@then("the first message of every request is the identical system message")
def _first_is_system(run_bodies):
    first0 = run_bodies[0]["messages"][0]
    assert first0["role"] == "system"
    for b in run_bodies:
        assert b["messages"][0] == first0


# ── Scenario 3: cross-run stability ──────────────────────────────────────────
@given("a second AsyncOrchestrator built with the same configuration", target_fixture="orch2")
def _orch2():
    return AsyncOrchestrator(skill_router=None, mcp=None, max_steps=3)


@when(parsers.parse('react_execute runs the goal "{goal}" on each orchestrator'),
      target_fixture="two_run_bodies")
def _run_two(orch, orch2, goal):
    import asyncio
    async def _run_async():
        # Independent recordings for each orchestrator so order can't bleed.
        bodies_a: list[dict] = []
        bodies_b: list[dict] = []

        def _make(recorder):
            responses = [
                _tool_call_msg("run_bash", '{"command":"ls"}'),
                _tool_call_msg("finish", '{"summary":"done"}'),
            ]
            async def _fake(*a, **k):
                recorder.append({"messages": [dict(m) for m in k["messages"]],
                                 "tools": k.get("tools", [])})
                return responses[min(len(recorder) - 1, len(responses) - 1)]
            return _fake

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new=AsyncMock(side_effect=_make(bodies_a))):
            await orch.react_execute(goal)
        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new=AsyncMock(side_effect=_make(bodies_b))):
            await orch2.react_execute(goal)
        return bodies_a, bodies_b

    return asyncio.run(_run_async())


@then("the tools list sent by both orchestrators is identical in order and content")
def _tools_cross_run(two_run_bodies):
    a, b = two_run_bodies
    assert a[0]["tools"] == b[0]["tools"]


@then("the serialized system+tools prefix is byte-identical between the two runs")
def _prefix_cross_run(two_run_bodies):
    a, b = two_run_bodies
    assert _prefix_of(a[0]) == _prefix_of(b[0])
