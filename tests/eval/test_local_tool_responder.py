import json
from eval.seq_ab.local_tool_responder import (
    handle_event_frame,
    result_stream_key,
    events_stream_key,
)


def test_keys():
    assert events_stream_key("t1") == "labmate:events:t1"
    assert result_stream_key("t1") == "labmate:tool-results:t1"


def test_non_tool_request_returns_none():
    assert handle_event_frame({"type": "answer.delta", "text": "hi"}, "/tmp") is None


def test_write_then_read_roundtrip(tmp_path):
    ws = str(tmp_path)
    # write_file
    w = handle_event_frame(
        {"type": "tool.request", "tool_request_id": "r1", "name": "write_file",
         "args": {"path": "sub/f.py", "content": "print(1)\n"}},
        ws,
    )
    assert w["tool_request_id"] == "r1"
    assert w["error"] is None
    assert (tmp_path / "sub" / "f.py").read_text() == "print(1)\n"
    # read_file sees it
    r = handle_event_frame(
        {"type": "tool.request", "tool_request_id": "r2", "name": "read_file",
         "args": {"path": "sub/f.py"}},
        ws,
    )
    assert r["result"]["content"] == "print(1)\n"


def test_bad_path_is_reported_as_error(tmp_path):
    out = handle_event_frame(
        {"type": "tool.request", "tool_request_id": "r3", "name": "write_file",
         "args": {"path": "../escape.py", "content": "x"}},
        str(tmp_path),
    )
    assert out["tool_request_id"] == "r3"
    assert out["error"] is not None
    assert out["result"] is None


def test_absolute_workspace_path_resolves(tmp_path):
    ws = str(tmp_path)
    out = handle_event_frame(
        {"type": "tool.request", "tool_request_id": "r4", "name": "write_file",
         "args": {"path": f"{ws}/abs.py", "content": "ok"}},
        ws,
    )
    assert out["error"] is None
    assert (tmp_path / "abs.py").read_text() == "ok"
