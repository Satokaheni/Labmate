from __future__ import annotations

from pathlib import Path

import pytest

from services.cli import local_tool_executor
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


# ── search_files ───────────────────────────────────────────────────────────────


def _relfile(hit: dict) -> str:
    """Normalize a hit's file path: ripgrep emits a './' prefix (cwd=root, searches '.'),
    the Python fallback does not — strip it so assertions hold under both backends."""
    f = hit["file"]
    return f[2:] if f.startswith("./") else f


def test_search_files_finds_matches_across_files(tmp_path: Path):
    (tmp_path / "a.txt").write_text("line1\n# TODO fix this\nline3\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("TODO other\nmore\n", encoding="utf-8")
    out = execute_local_tool("search_files", {"query": "TODO"}, workspace=str(tmp_path))
    assert out["count"] == 2
    files = {_relfile(h) for h in out["hits"]}
    assert files == {"a.txt", "b.txt"}
    by_file = {_relfile(h): h for h in out["hits"]}
    assert by_file["a.txt"]["line"] == 2
    assert "TODO" in by_file["a.txt"]["text"]
    assert by_file["b.txt"]["line"] == 1


def test_search_files_path_scoping(tmp_path: Path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "inner.txt").write_text("NEEDLE here\n", encoding="utf-8")
    (tmp_path / "outer.txt").write_text("NEEDLE there\n", encoding="utf-8")
    out = execute_local_tool(
        "search_files", {"query": "NEEDLE", "path": "sub"}, workspace=str(tmp_path)
    )
    assert out["count"] == 1
    assert _relfile(out["hits"][0]) == "inner.txt"


def test_search_files_glob_filters_by_extension(tmp_path: Path):
    (tmp_path / "match.py").write_text("MARKER in py\n", encoding="utf-8")
    (tmp_path / "match.txt").write_text("MARKER in txt\n", encoding="utf-8")
    out = execute_local_tool(
        "search_files", {"query": "MARKER", "glob": "*.py"}, workspace=str(tmp_path)
    )
    assert out["count"] == 1
    assert _relfile(out["hits"][0]) == "match.py"


def test_search_files_max_results_caps_hits(tmp_path: Path):
    (tmp_path / "many.txt").write_text("\n".join(f"HIT{i}" for i in range(10)), encoding="utf-8")
    out = execute_local_tool(
        "search_files", {"query": "HIT", "max_results": 3}, workspace=str(tmp_path)
    )
    assert out["count"] == 3
    assert len(out["hits"]) == 3


def test_search_files_no_match_returns_empty(tmp_path: Path):
    (tmp_path / "a.txt").write_text("nothing interesting\n", encoding="utf-8")
    out = execute_local_tool("search_files", {"query": "NOPE_NOT_FOUND"}, workspace=str(tmp_path))
    assert out == {"hits": [], "count": 0}


def test_search_files_empty_query_returns_empty(tmp_path: Path):
    out = execute_local_tool("search_files", {"query": "   "}, workspace=str(tmp_path))
    assert out == {"hits": [], "count": 0}


def test_search_files_path_escape_is_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="outside workspace"):
        execute_local_tool("search_files", {"query": "x", "path": "../.."}, workspace=str(tmp_path))


def test_search_files_forced_python_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Force the Python-walk fallback by hiding ripgrep, and confirm it still finds hits."""
    monkeypatch.setattr(local_tool_executor.shutil, "which", lambda _: None)
    (tmp_path / "a.txt").write_text("line1\nFALLBACK_NEEDLE\nline3\n", encoding="utf-8")
    out = execute_local_tool("search_files", {"query": "FALLBACK_NEEDLE"}, workspace=str(tmp_path))
    assert out["count"] == 1
    assert out["hits"][0] == {"file": "a.txt", "line": 2, "text": "FALLBACK_NEEDLE"}


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

    from services.cli.event_stream import _ToolInterceptingStream

    class _FakeStream:
        """Minimal duck-typed stream: _ToolInterceptingStream only needs .events()."""

        def __init__(self, evs):
            self._evs = evs

        async def events(self):
            for e in self._evs:
                yield e

        async def aclose(self) -> None:
            pass

    calls: list[tuple] = []

    async def fake_send_result(tool_request_id, result, error):
        calls.append((tool_request_id, result, error))

    stream = _FakeStream(events_list)
    interceptor = _ToolInterceptingStream(stream, fake_send_result, str(tmp_path))
    seen = []
    async for ev in interceptor.events():
        seen.append(ev["type"])

    assert seen == ["turn.start", "tool.request", "turn.done"]
    assert len(calls) == 1
    assert calls[0] == ("req-cb", {"content": "content"}, None)
