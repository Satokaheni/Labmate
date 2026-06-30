import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.orchestrator import events


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


@pytest.mark.asyncio
async def test_emitter_xadds_event_with_envelope():
    r = MagicMock()
    r.xadd = AsyncMock()
    em = events.EventEmitter(r, "task-123")
    await em.emit("tool.start", name="pdf-parse", kind="skill")
    assert r.xadd.await_count == 1
    stream, fields = r.xadd.await_args.args[0], r.xadd.await_args.args[1]
    assert stream == "labmate:events:task-123"
    evt = json.loads(fields["event"])
    assert evt["type"] == "tool.start"
    assert evt["task_id"] == "task-123"
    assert evt["seq"] == 1
    assert evt["name"] == "pdf-parse" and evt["kind"] == "skill"
    assert "ts" in evt


@pytest.mark.asyncio
async def test_emitter_seq_increments_and_failure_is_swallowed():
    r = MagicMock()
    r.xadd = AsyncMock(side_effect=[None, RuntimeError("redis down")])
    em = events.EventEmitter(r, "t")
    await em.emit("turn.start")
    await em.emit("turn.done")  # must NOT raise
    assert em._seq == 2


@pytest.mark.asyncio
async def test_module_emit_is_noop_without_contextvar():
    events.current_emitter.set(None)
    await events.emit("tool.start", name="x")  # no exception = pass


@pytest.mark.asyncio
async def test_module_emit_uses_contextvar_emitter():
    r = MagicMock()
    r.xadd = AsyncMock()
    em = events.EventEmitter(r, "ctx-task")
    token = events.current_emitter.set(em)
    try:
        await events.emit("reasoning", node="route", text="why")
    finally:
        events.current_emitter.reset(token)
    assert r.xadd.await_count == 1


@pytest.mark.asyncio
async def test_handle_emits_agent_status_active_and_idle():
    """_handle must emit agent_status active before run_task and idle in finally."""
    import json
    from unittest.mock import AsyncMock, MagicMock

    import fakeredis.aioredis

    from services.orchestrator.main import OrchestratorProcess

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
    mock_storage.consolidator = MagicMock()
    mock_storage.consolidator.on_task_complete = AsyncMock()
    mock_storage.consolidator.write_reflections = AsyncMock()

    proc = OrchestratorProcess()
    proc._redis = r

    await proc._handle("msg-1", fields, mock_orch, mock_storage)

    entries = await r.xrange("labmate:events:t-agent-status")
    event_types = [json.loads(f["event"])["type"] for _, f in entries]
    assert "agent_status" in event_types

    agent_status_events = [
        json.loads(f["event"])
        for _, f in entries
        if json.loads(f["event"]).get("type") == "agent_status"
    ]
    states = [e["status"]["brain"]["state"] for e in agent_status_events]
    assert "active" in states
    assert "idle" in states


@pytest.mark.asyncio
async def test_is_cancelled_returns_true_when_flag_set():
    import fakeredis.aioredis

    from services.orchestrator.events import is_cancelled

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await r.set("labmate:cancel:task-x", "1", ex=60)
    assert await is_cancelled(r, "task-x") is True


@pytest.mark.asyncio
async def test_is_cancelled_returns_false_when_no_flag():
    import fakeredis.aioredis

    from services.orchestrator.events import is_cancelled

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    assert await is_cancelled(r, "task-y") is False


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
