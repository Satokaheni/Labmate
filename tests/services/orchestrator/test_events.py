import json
import pytest
from unittest.mock import AsyncMock, MagicMock
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
