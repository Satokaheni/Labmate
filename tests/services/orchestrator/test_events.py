from unittest.mock import MagicMock

import pytest

from services.orchestrator import events
from services.orchestrator.inproc_bus import EventBus


def test_extract_reasoning_returns_reasoning_content():
    msg = MagicMock()
    msg.reasoning_content = "because the task matches pdf-parse"
    resp = MagicMock()
    resp.choices = [MagicMock(message=msg)]
    assert events.extract_reasoning(resp) == "because the task matches pdf-parse"


def test_extract_reasoning_missing_is_empty():
    resp = MagicMock()
    resp.choices = []
    assert events.extract_reasoning(resp) == ""


def test_reasoning_summary_first_line_truncated():
    assert events.reasoning_summary("line one\nline two") == "line one"
    assert len(events.reasoning_summary("x" * 500)) == 120


async def _drain_one(sub, timeout: float = 1.0):
    import asyncio

    return await asyncio.wait_for(sub.__anext__(), timeout=timeout)


@pytest.mark.asyncio
async def test_emitter_publishes_event_with_envelope():
    bus = EventBus()
    sub = bus.subscribe("events:task-123")
    em = events.EventEmitter(bus, "task-123")
    await em.emit("tool.start", name="pdf-parse", kind="skill")
    evt = await _drain_one(sub)
    assert evt["type"] == "tool.start"
    assert evt["task_id"] == "task-123"
    assert evt["seq"] == 1
    assert evt["name"] == "pdf-parse" and evt["kind"] == "skill"
    assert "ts" in evt
    sub.close()


@pytest.mark.asyncio
async def test_emitter_seq_increments_and_failure_is_swallowed():
    bus = MagicMock()
    bus.publish = MagicMock(side_effect=[None, RuntimeError("bus down")])
    em = events.EventEmitter(bus, "t")
    await em.emit("turn.start")
    await em.emit("turn.done")  # must NOT raise
    assert em._seq == 2


@pytest.mark.asyncio
async def test_module_emit_is_noop_without_contextvar():
    events.current_emitter.set(None)
    await events.emit("tool.start", name="x")  # no exception = pass


@pytest.mark.asyncio
async def test_module_emit_uses_contextvar_emitter():
    bus = EventBus()
    sub = bus.subscribe("events:ctx-task")
    em = events.EventEmitter(bus, "ctx-task")
    token = events.current_emitter.set(em)
    try:
        await events.emit("reasoning", node="route", text="why")
    finally:
        events.current_emitter.reset(token)
    evt = await _drain_one(sub)
    assert evt["type"] == "reasoning"
    sub.close()


@pytest.mark.asyncio
async def test_handle_emits_agent_status_active_and_idle():
    """_handle must emit agent_status active before run_task and idle in finally."""
    import json
    from unittest.mock import AsyncMock, MagicMock

    import fakeredis.aioredis

    from services.orchestrator.main import OrchestratorProcess

    # Goal-loop transport (xack/write_result) still uses Redis until T5;
    # only the event stream itself has moved to the in-process bus.
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)

    fields = {
        "payload": json.dumps(
            {
                "task_id": "t-agent-status",
                "task": "hello",
                "session_id": "s-1",
                "user_id": "",
                "workspace_id": "",
            }
        )
    }

    mock_orch = MagicMock()
    mock_orch.run_task = AsyncMock(return_value={"final_answer": "hi", "error": None})
    mock_orch.stream_final_answer = AsyncMock(return_value="hi")

    mock_storage = MagicMock()
    mock_storage.workspaces = MagicMock()
    mock_storage.workspaces.record_session = AsyncMock()
    mock_storage.workspaces.upsert_workspace = AsyncMock()
    mock_storage.workspaces.complete_session = AsyncMock()

    proc = OrchestratorProcess()
    proc._redis = r
    sub = proc.bus.subscribe("events:t-agent-status")

    await proc._handle("msg-1", fields, mock_orch, mock_storage)

    import asyncio

    event_types = []
    agent_status_events = []
    while True:
        try:
            evt = await asyncio.wait_for(sub.__anext__(), timeout=0.2)
        except TimeoutError:
            break
        event_types.append(evt["type"])
        if evt["type"] == "agent_status":
            agent_status_events.append(evt)
    sub.close()

    assert "agent_status" in event_types
    states = [e["status"]["brain"]["state"] for e in agent_status_events]
    assert "active" in states
    assert "idle" in states


@pytest.mark.asyncio
async def test_is_cancelled_returns_true_when_flag_set():
    from services.orchestrator.events import is_cancelled
    from services.orchestrator.inproc_bus import SignalRegistry

    signals = SignalRegistry()
    signals.request_cancel("task-x")
    assert await is_cancelled(signals, "task-x") is True


@pytest.mark.asyncio
async def test_is_cancelled_returns_false_when_no_flag():
    from services.orchestrator.events import is_cancelled
    from services.orchestrator.inproc_bus import SignalRegistry

    signals = SignalRegistry()
    assert await is_cancelled(signals, "task-y") is False


def test_tool_event_display_load_skill_shows_loaded_skill_name():
    """load_skill must surface the LOADED skill (not the mechanism) as kind='skill'."""
    from services.orchestrator.events import tool_event_display

    assert tool_event_display("load_skill", {"name": "repo-greeting"}) == ("skill", "repo-greeting")


def test_tool_event_display_call_skill_tool_shows_skill():
    from services.orchestrator.events import tool_event_display

    assert tool_event_display("call_skill_tool", {"skill": "ast-search", "tool": "find"}) == (
        "skill",
        "ast-search",
    )


def test_tool_event_display_plain_tool_is_kind_tool():
    from services.orchestrator.events import tool_event_display

    assert tool_event_display("read_file", {"path": "a.py"}) == ("tool", "read_file")
    assert tool_event_display("run_tests", {}) == ("tool", "run_tests")


def test_tool_event_display_falls_back_when_arg_missing():
    """Missing/empty expected arg falls back to the mechanism name (never crashes)."""
    from services.orchestrator.events import tool_event_display

    assert tool_event_display("load_skill", {}) == ("skill", "load_skill")
    assert tool_event_display("load_skill", None) == ("skill", "load_skill")
    assert tool_event_display("call_skill_tool", {"skill": ""}) == ("skill", "call_skill_tool")


def test_tool_event_display_mcp_tool_shows_server_as_skill():
    """A hosted mcp__<server>__<tool> call surfaces the SERVER as kind='skill'."""
    from services.orchestrator.events import tool_event_display

    assert tool_event_display("mcp__codegraph__codegraph_status", {}) == ("skill", "codegraph")
    assert tool_event_display("mcp__ast-ts-refactor__find_references", {"symbol": "x"}) == (
        "skill",
        "ast-ts-refactor",
    )
