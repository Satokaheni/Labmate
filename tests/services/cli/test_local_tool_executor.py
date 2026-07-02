from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from services.cli.local_tool_executor import execute_local_tool

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
        execute_local_tool("read_file", {"path": "../../../etc/passwd"}, workspace=str(tmp_path))


def test_absolute_path_escape_is_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="outside workspace"):
        execute_local_tool("read_file", {"path": "/etc/passwd"}, workspace=str(tmp_path))


def test_unknown_tool_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="unknown local tool"):
        execute_local_tool("delete_everything", {}, workspace=str(tmp_path))


def test_read_file_offset_and_limit(tmp_path: Path):
    """offset=2, limit=2 on a 5-line file returns lines 2-3 (1-based)."""
    content = "line1\nline2\nline3\nline4\nline5\n"
    (tmp_path / "f.txt").write_text(content, encoding="utf-8")
    out = execute_local_tool(
        "read_file", {"path": "f.txt", "offset": 2, "limit": 2}, workspace=str(tmp_path)
    )
    assert out == {"content": "line2\nline3\n"}


def test_read_file_whole_file_when_no_offset_or_limit(tmp_path: Path):
    """Omitting offset and limit returns the whole file unchanged (backward compat)."""
    content = "line1\nline2\nline3\nline4\nline5\n"
    (tmp_path / "f.txt").write_text(content, encoding="utf-8")
    out = execute_local_tool("read_file", {"path": "f.txt"}, workspace=str(tmp_path))
    assert out == {"content": content}


def test_read_file_offset_past_eof(tmp_path: Path):
    """offset past EOF returns empty content string."""
    content = "line1\nline2\n"
    (tmp_path / "f.txt").write_text(content, encoding="utf-8")
    out = execute_local_tool("read_file", {"path": "f.txt", "offset": 100}, workspace=str(tmp_path))
    assert out == {"content": ""}


def test_read_file_exotic_line_separator_parity(tmp_path: Path):
    """Files with vertical tab (\\x0b) should NOT split on it (parity with TS).

    TS splits on \\n only; Python must do the same.
    Content: "a\\x0bb\\nc" → 2 lines: ["a\\x0bb\\n", "c"]
    offset=1, limit=1 → line 1 → "a\\x0bb\\n"
    """
    content = "a\x0bb\nc"
    (tmp_path / "f.txt").write_text(content, encoding="utf-8")
    out = execute_local_tool(
        "read_file", {"path": "f.txt", "offset": 1, "limit": 1}, workspace=str(tmp_path)
    )
    # TS splits "a\x0bb\nc" on \n → ["a\x0bb", "c"]
    # TS restores newlines → ["a\x0bb\n", "c"]
    # TS slices [0:1] → ["a\x0bb\n"]
    # TS joins → "a\x0bb\n"
    assert out == {"content": "a\x0bb\n"}


# ── _ToolInterceptingStream integration ───────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_intercepting_stream_calls_send_result_callback(tmp_path: Path):
    """_ToolInterceptingStream calls the send_result callback and yields all events."""
    (tmp_path / "readme.txt").write_text("content", encoding="utf-8")

    events_list = [
        {"type": "turn.start", "seq": 0},
        {
            "type": "tool.request",
            "tool_request_id": "req-cb",
            "name": "read_file",
            "args": {"path": "readme.txt"},
        },
        {"type": "turn.done", "status": "complete", "seq": 2},
    ]

    async def _fake_gen(evs):
        for e in evs:
            yield e

    from services.cli.event_stream import EventStream, _ToolInterceptingStream

    calls: list[tuple] = []

    async def fake_send_result(tool_request_id, result, error):
        calls.append((tool_request_id, result, error))

    with patch("services.cli.redis_event_stream.tail_events", return_value=_fake_gen(events_list)):
        stream = EventStream("redis://x", "t-x")
        _ = await stream.first(timeout=1.0)

        interceptor = _ToolInterceptingStream(stream, fake_send_result, str(tmp_path))
        seen = []
        async for ev in interceptor.events():
            seen.append(ev["type"])

    assert seen == ["turn.start", "tool.request", "turn.done"]
    assert len(calls) == 1
    assert calls[0] == ("req-cb", {"content": "content"}, None)
