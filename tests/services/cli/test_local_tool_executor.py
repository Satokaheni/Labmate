from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import fakeredis.aioredis
import pytest

from services.cli.local_tool_executor import (
    TOOL_RESULTS_PREFIX,
    execute_local_tool,
    handle_tool_request,
)


@pytest.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


# ── execute_local_tool ─────────────────────────────────────────────────────────

def test_read_file_returns_content(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    out = execute_local_tool("read_file", {"path": "a.txt"}, workspace=str(tmp_path))
    assert out == {"content": "hello"}


def test_write_file_creates_file(tmp_path: Path):
    out = execute_local_tool(
        "write_file", {"path": "sub/b.txt", "content": "data"}, workspace=str(tmp_path)
    )
    assert out["ok"] is True
    assert (tmp_path / "sub" / "b.txt").read_text(encoding="utf-8") == "data"


def test_list_dir_lists_entries(tmp_path: Path):
    (tmp_path / "x.txt").write_text("1", encoding="utf-8")
    (tmp_path / "d").mkdir()
    out = execute_local_tool("list_dir", {"path": "."}, workspace=str(tmp_path))
    assert set(out["entries"]) == {"x.txt", "d"}


def test_path_escape_is_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="outside workspace"):
        execute_local_tool(
            "read_file", {"path": "../../../etc/passwd"}, workspace=str(tmp_path)
        )


def test_absolute_path_escape_is_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="outside workspace"):
        execute_local_tool("read_file", {"path": "/etc/passwd"}, workspace=str(tmp_path))


def test_unknown_tool_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="unknown local tool"):
        execute_local_tool("delete_everything", {}, workspace=str(tmp_path))


# ── handle_tool_request ────────────────────────────────────────────────────────

async def test_handle_tool_request_writes_result(redis, tmp_path: Path):
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    ev = {
        "type": "tool.request",
        "task_id": "task-1",
        "tool_request_id": "req-1",
        "name": "read_file",
        "args": {"path": "a.txt"},
    }
    await handle_tool_request(redis, ev, workspace=str(tmp_path))
    entries = await redis.xrange(f"{TOOL_RESULTS_PREFIX}task-1")
    assert len(entries) == 1
    frame = json.loads(entries[0][1]["result"])
    assert frame == {"tool_request_id": "req-1", "result": {"content": "hi"}, "error": None}


async def test_handle_tool_request_reports_error(redis, tmp_path: Path):
    ev = {
        "type": "tool.request",
        "task_id": "task-1",
        "tool_request_id": "req-2",
        "name": "read_file",
        "args": {"path": "../escape"},
    }
    await handle_tool_request(redis, ev, workspace=str(tmp_path))
    entries = await redis.xrange(f"{TOOL_RESULTS_PREFIX}task-1")
    assert len(entries) == 1
    frame = json.loads(entries[0][1]["result"])
    assert frame["tool_request_id"] == "req-2"
    assert frame["result"] is None
    assert "outside workspace" in frame["error"]


# ── _ToolInterceptingStream integration ───────────────────────────────────────

async def test_tool_intercepting_stream_handles_tool_request(redis, tmp_path: Path):
    """The interceptor calls handle_tool_request and yields all events."""
    (tmp_path / "readme.txt").write_text("content", encoding="utf-8")

    events_list = [
        {"type": "turn.start", "seq": 0},
        {
            "type": "tool.request",
            "task_id": "t-x",
            "tool_request_id": "req-intercept",
            "name": "read_file",
            "args": {"path": "readme.txt"},
        },
        {"type": "turn.done", "status": "complete", "seq": 2},
    ]

    async def _fake_gen(evs):
        for e in evs:
            yield e

    from services.cli.event_stream import EventStream, _ToolInterceptingStream

    with patch("services.cli.event_stream.tail_events", return_value=_fake_gen(events_list)):
        stream = EventStream("redis://x", "t-x")
        _ = await stream.first(timeout=1.0)  # consume first into buffer

        interceptor = _ToolInterceptingStream(stream, redis, str(tmp_path))
        seen = []
        async for ev in interceptor.events():
            seen.append(ev["type"])

    # All events were yielded (including tool.request)
    assert seen == ["turn.start", "tool.request", "turn.done"]

    # The tool result was written to Redis
    entries = await redis.xrange(f"{TOOL_RESULTS_PREFIX}t-x")
    assert len(entries) == 1
    frame = json.loads(entries[0][1]["result"])
    assert frame["tool_request_id"] == "req-intercept"
    assert frame["result"] == {"content": "content"}
    assert frame["error"] is None
