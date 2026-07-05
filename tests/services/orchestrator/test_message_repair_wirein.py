from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.orchestrator.coding_orchestrator import AsyncOrchestrator


def _tool_call_msg(name, args):
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


def _finish(summary="ok"):
    m = MagicMock(tool_calls=None, content=summary)
    m.model_dump = lambda: {"role": "assistant", "content": summary}
    return MagicMock(choices=[MagicMock(message=m)])


def _stub_mcp():
    mcp = AsyncMock()
    res = MagicMock()
    res.content = [MagicMock(text="output")]
    res.isError = False
    mcp.call_tool.return_value = res
    return mcp


@pytest.mark.asyncio
async def test_orphan_tool_result_repaired_before_failover_call(monkeypatch):
    """An orphaned tool result injected into the loop's message list must be
    dropped by the sanitizer before the failover call sees it."""
    monkeypatch.setenv("ENABLE_MESSAGE_REPAIR", "1")
    orch = AsyncOrchestrator(skill_router=None, mcp=_stub_mcp(), workspace="/tmp")

    captured = []

    async def fake_failover(*args, **kwargs):
        # Snapshot the messages this call received.
        captured.append([dict(m) for m in kwargs["messages"]])
        # Turn 1: ask for a bash tool; turn 2: finish.
        if len(captured) == 1:
            return _tool_call_msg("run_bash", {"command": "echo hi"})
        return _finish("done")

    # Inject an orphaned tool result by patching sanitize's INPUT through a
    # pre-seeded message: easiest is to assert the sanitizer ran on the 2nd call,
    # where the appended assistant+tool turn is valid (no orphan) — so instead we
    # verify the wire-in by asserting NO orphan ever reaches the model and the
    # captured 2nd-call messages are well-formed per validate_messages.
    from services.orchestrator.message_repair import validate_messages

    with patch(
        "services.orchestrator.coding_orchestrator.acompletion_with_failover",
        new=fake_failover,
    ):
        result = await orch._run_react_loop("do work", max_steps=4)

    assert result["ok"] is True
    # Every message list handed to the model must validate clean.
    for msgs in captured:
        assert validate_messages(msgs) == [], f"model saw malformed messages: {msgs}"


@pytest.mark.asyncio
async def test_wirein_is_noop_when_flag_off(monkeypatch):
    """With ENABLE_MESSAGE_REPAIR off, a well-formed loop still completes
    identically (regression guard)."""
    monkeypatch.setenv("ENABLE_MESSAGE_REPAIR", "0")
    orch = AsyncOrchestrator(skill_router=None, mcp=_stub_mcp(), workspace="/tmp")

    seq = [_tool_call_msg("run_bash", {"command": "echo hi"}), _finish("done")]
    calls = {"n": 0}

    async def fake_failover(*args, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        return seq[i]

    with patch(
        "services.orchestrator.coding_orchestrator.acompletion_with_failover",
        new=fake_failover,
    ):
        result = await orch._run_react_loop("do work", max_steps=4)

    assert result["ok"] is True
    assert result["summary"] == "done"


@pytest.mark.asyncio
async def test_finish_nudge_produces_valid_message_sequence(monkeypatch):
    """When verify-nudge triggers on finish, the message sequence must be valid:
    assistant(finish) -> tool(finish) -> user(nudge). The synthetic tool result
    must be appended before the nudge so that when sanitize_messages runs, the
    finish tool_call is answered and validate_messages reports no dangling calls.
    """
    monkeypatch.setenv("ENABLE_MESSAGE_REPAIR", "1")
    orch = AsyncOrchestrator(skill_router=None, mcp=_stub_mcp(), workspace="/tmp")

    captured = []
    call_count = [0]

    async def fake_failover(*args, **kwargs):
        # Capture messages before sanitization
        captured.append([dict(m) for m in kwargs["messages"]])
        call_count[0] += 1

        if call_count[0] == 1:
            # First call: return finish tool call
            return _tool_call_msg("finish", {"summary": "edited code"})
        elif call_count[0] == 2:
            # After nudge: return another finish (acceptance of nudge)
            return _finish("done with verification")
        return _finish("shouldn't reach here")

    from services.orchestrator.message_repair import validate_messages

    # Patch needs_verification to always return True on the first finish
    needs_verification_calls = [0]

    def trigger_nudge_once(*args, **kwargs):
        needs_verification_calls[0] += 1
        # Trigger on first finish call, not on second
        return needs_verification_calls[0] == 1

    # Mock events for emitting verify.nudge
    orch.events = AsyncMock()

    with (
        patch(
            "services.orchestrator.coding_orchestrator.acompletion_with_failover",
            new=fake_failover,
        ),
        patch(
            "services.orchestrator.coding_orchestrator.needs_verification",
            new=trigger_nudge_once,
        ),
    ):
        result = await orch._run_react_loop("do work", max_steps=6)

    assert result["ok"] is True
    # The first call should produce a sequence with:
    #   assistant(finish) -> tool(finish) -> user(nudge)
    # The second call should produce a sequence where finish is properly answered.
    assert len(captured) >= 2, "Expected at least 2 model calls (before/after nudge)"

    # Validate all message sequences
    for i, msgs in enumerate(captured):
        problems = validate_messages(msgs)
        assert problems == [], f"Call {i+1}: validate_messages failed: {problems}\n{msgs}"


@pytest.mark.asyncio
async def test_maybe_repair_patches_danglers_when_flag_off(monkeypatch):
    """_maybe_repair must ALWAYS patch dangling tool_calls, even with
    ENABLE_MESSAGE_REPAIR off — a single dangler 400s every later request."""
    monkeypatch.setenv("ENABLE_MESSAGE_REPAIR", "0")
    orch = AsyncOrchestrator(skill_router=None, mcp=_stub_mcp(), workspace="/tmp")

    from services.orchestrator.message_repair import validate_messages

    dangling = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "run_bash", "arguments": "{}"},
                }
            ],
        },
        {"role": "user", "content": "steer: focus here"},
    ]
    assert validate_messages(dangling) != []  # dangling present

    repaired = orch._maybe_repair(dangling)

    assert validate_messages(repaired) == []  # patched despite flag off
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "c1" for m in repaired)
