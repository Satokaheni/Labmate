import json
import pytest
import fakeredis.aioredis
from unittest.mock import AsyncMock, MagicMock, patch

from services.orchestrator.coding_orchestrator import AsyncOrchestrator
from services.orchestrator import events
from services.orchestrator.steer_inject import OOB_OPEN


def _bash_then_finish():
    """Two model responses: turn 1 calls run_bash; turn 2 calls finish."""
    def _mk_bash():
        tc = MagicMock()
        tc.id = "c1"
        tc.function = MagicMock()
        tc.function.name = "run_bash"
        tc.function.arguments = json.dumps({"command": "ls"})
        msg = MagicMock()
        msg.content = None
        msg.tool_calls = [tc]
        msg.reasoning_content = ""
        msg.model_dump = lambda: {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "run_bash", "arguments": "{}"}}],
        }
        return MagicMock(choices=[MagicMock(message=msg)])

    def _mk_finish():
        tc = MagicMock()
        tc.id = "c2"
        tc.function = MagicMock()
        tc.function.name = "finish"
        tc.function.arguments = json.dumps({"summary": "done"})
        msg = MagicMock()
        msg.content = None
        msg.tool_calls = [tc]
        msg.reasoning_content = ""
        msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
        return MagicMock(choices=[MagicMock(message=msg)])

    return [_mk_bash(), _mk_finish()]


def _always_bash():
    def _mk():
        tc = MagicMock()
        tc.id = "c"
        tc.function = MagicMock()
        tc.function.name = "run_bash"
        tc.function.arguments = json.dumps({"command": "ls"})
        msg = MagicMock()
        msg.content = None
        msg.tool_calls = [tc]
        msg.reasoning_content = ""
        msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
        return MagicMock(choices=[MagicMock(message=msg)])
    return _mk


@pytest.fixture
def orch_with_redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    orch = AsyncOrchestrator(skill_router=None, mcp=AsyncMock(), workspace="/tmp", max_steps=6)
    bash_result = MagicMock()
    bash_result.content = [MagicMock(text="files")]
    bash_result.isError = False
    orch.mcp.call_tool = AsyncMock(return_value=bash_result)
    orch.redis = r
    return orch, r


async def _with_task(task_id, coro_fn):
    """Run coro_fn() with an active EventEmitter so current_task_id() works."""
    em = events.EventEmitter(MagicMock(), task_id)
    em.emit = AsyncMock()  # swallow event emission in unit tests
    token = events.current_emitter.set(em)
    try:
        return await coro_fn()
    finally:
        events.current_emitter.reset(token)


@pytest.mark.asyncio
async def test_steer_injected_on_next_turn(orch_with_redis):
    orch, r = orch_with_redis
    captured = []

    async def _capture(*a, **k):
        # Record the messages seen on each model call, then return the scripted resp.
        captured.append([dict(m) for m in k["messages"]])
        return _capture.responses.pop(0)
    _capture.responses = _bash_then_finish()

    await events.write_steer(r, "t-steer", "work on db.py instead")

    async def _run():
        with patch("services.orchestrator.coding_orchestrator.acompletion_with_failover",
                   new=AsyncMock(side_effect=_capture)):
            return await orch._run_react_loop("refactor", 6)

    await _with_task("t-steer", _run)

    # The SECOND model call must carry the out-of-band user message.
    assert len(captured) == 2
    second_blob = json.dumps(captured[1])
    # Check for both the OOB marker prefix and the steer text
    assert "OUT-OF-BAND USER MESSAGE" in second_blob
    assert "work on db.py instead" in second_blob
    assert "[/OUT-OF-BAND USER MESSAGE]" in second_blob
    # Consumed exactly once — the key is gone.
    assert await r.exists("labmate:steer:t-steer") == 0


@pytest.mark.asyncio
async def test_cancel_halts_with_partial_summary(orch_with_redis):
    orch, r = orch_with_redis
    calls = {"n": 0}

    async def _count(*a, **k):
        calls["n"] += 1
        # Cancel arrives after the first model call, before the second turn-top check.
        if calls["n"] == 1:
            pass  # cancel is already set in redis
        return _always_bash()()

    await r.set("labmate:cancel:t-cancel", "1", ex=60)

    async def _run():
        with patch("services.orchestrator.coding_orchestrator.acompletion_with_failover",
                   new=AsyncMock(side_effect=_count)):
            return await orch._run_react_loop("long job", 6)

    result = await _with_task("t-cancel", _run)
    assert result["ok"] is False
    assert "cancel" in result["summary"].lower()
    # Halted at the very first turn-top check → model never called.
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_no_steer_no_cancel_unchanged(orch_with_redis):
    orch, r = orch_with_redis

    async def _finish(*a, **k):
        tc = MagicMock(); tc.id = "c"; tc.function = MagicMock()
        tc.function.name = "finish"
        tc.function.arguments = json.dumps({"summary": "all done"})
        msg = MagicMock(); msg.content = None; msg.tool_calls = [tc]
        msg.reasoning_content = ""
        msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
        return MagicMock(choices=[MagicMock(message=msg)])

    async def _run():
        with patch("services.orchestrator.coding_orchestrator.acompletion_with_failover",
                   new=AsyncMock(side_effect=_finish)):
            return await orch._run_react_loop("trivial", 6)

    result = await _with_task("t-plain", _run)
    assert result["ok"] is True
    assert result["summary"] == "all done"


def _bash_read_write_then_finish():
    """Four model responses: turn 1 calls run_bash, turn 2 calls read_file,
    turn 3 calls write_file, turn 4 calls finish. Different tools to avoid
    loop detection."""
    def _mk_bash():
        tc = MagicMock()
        tc.id = "c1"
        tc.function = MagicMock()
        tc.function.name = "run_bash"
        tc.function.arguments = json.dumps({"command": "ls"})
        msg = MagicMock()
        msg.content = None
        msg.tool_calls = [tc]
        msg.reasoning_content = ""
        msg.model_dump = lambda: {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "run_bash", "arguments": "{}"}}],
        }
        return MagicMock(choices=[MagicMock(message=msg)])

    def _mk_read():
        tc = MagicMock()
        tc.id = "c2"
        tc.function = MagicMock()
        tc.function.name = "read_file"
        tc.function.arguments = json.dumps({"path": "/tmp/test.py"})
        msg = MagicMock()
        msg.content = None
        msg.tool_calls = [tc]
        msg.reasoning_content = ""
        msg.model_dump = lambda: {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "c2", "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"}}],
        }
        return MagicMock(choices=[MagicMock(message=msg)])

    def _mk_write():
        tc = MagicMock()
        tc.id = "c3"
        tc.function = MagicMock()
        tc.function.name = "write_file"
        tc.function.arguments = json.dumps({"path": "/tmp/test2.py", "content": "# test"})
        msg = MagicMock()
        msg.content = None
        msg.tool_calls = [tc]
        msg.reasoning_content = ""
        msg.model_dump = lambda: {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "c3", "type": "function",
                            "function": {"name": "write_file", "arguments": "{}"}}],
        }
        return MagicMock(choices=[MagicMock(message=msg)])

    def _mk_finish():
        tc = MagicMock()
        tc.id = "c4"
        tc.function = MagicMock()
        tc.function.name = "finish"
        tc.function.arguments = json.dumps({"summary": "done"})
        msg = MagicMock()
        msg.content = None
        msg.tool_calls = [tc]
        msg.reasoning_content = ""
        msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
        return MagicMock(choices=[MagicMock(message=msg)])

    return [_mk_bash(), _mk_read(), _mk_write(), _mk_finish()]


@pytest.mark.asyncio
async def test_steer_injected_exactly_once_across_four_turns(orch_with_redis):
    """Regression test: steer should be injected exactly once on turn 2,
    not re-injected on turns 3+. This test runs 4 turns with pre-written steer
    and verifies the OOB steer text appears in exactly one captured message list."""
    orch, r = orch_with_redis
    captured = []

    async def _capture(*a, **k):
        # Record the messages seen on each model call, then return the scripted resp.
        captured.append([dict(m) for m in k["messages"]])
        return _capture.responses.pop(0)
    _capture.responses = _bash_read_write_then_finish()

    await events.write_steer(r, "t-steer-4t", "focus on error handling")

    async def _run():
        with patch("services.orchestrator.coding_orchestrator.acompletion_with_failover",
                   new=AsyncMock(side_effect=_capture)):
            return await orch._run_react_loop("refactor code", 6)

    await _with_task("t-steer-4t", _run)

    # We should have 4 model calls captured.
    assert len(captured) == 4

    # The steer text should appear in exactly ONE message list.
    turns_with_steer = []
    for turn_idx, msg_list in enumerate(captured):
        blob = json.dumps(msg_list)
        if "focus on error handling" in blob:
            turns_with_steer.append(turn_idx)

    assert len(turns_with_steer) == 1, (
        f"Steer text should appear exactly once, but found in {len(turns_with_steer)} "
        f"calls at turns {turns_with_steer}"
    )
    # The steer should appear on turn 2 (index 1)
    assert turns_with_steer[0] == 1, (
        f"Steer text should appear on turn 2 (index 1), but appeared at index {turns_with_steer[0]}"
    )

    # Verify the marker is also in turn 2
    second_blob = json.dumps(captured[1])
    assert "OUT-OF-BAND USER MESSAGE" in second_blob
    assert "[/OUT-OF-BAND USER MESSAGE]" in second_blob

    # Verify turns 3 and 4 do NOT have the steer text
    third_blob = json.dumps(captured[2])
    assert "focus on error handling" not in third_blob
    fourth_blob = json.dumps(captured[3])
    assert "focus on error handling" not in fourth_blob

    # Consumed exactly once — the key is gone.
    assert await r.exists("labmate:steer:t-steer-4t") == 0
