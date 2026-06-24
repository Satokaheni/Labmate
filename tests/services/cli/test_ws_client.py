# tests/services/cli/test_ws_client.py
from __future__ import annotations
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.cli.ws_client import LabmateWSClient, WSEventStream, _normalize_ws_event


# ── _normalize_ws_event ────────────────────────────────────────────────────────

def test_normalize_node_enter():
    ev = {"type": "node.enter", "turnId": "t-1", "node": "plan", "thinkingBudget": 5000}
    out = _normalize_ws_event(ev)
    assert out == {"type": "turn.start", "node": "plan", "thinking_budget": 5000}


def test_normalize_reasoning_delta():
    out = _normalize_ws_event({"type": "reasoning.delta", "turnId": "t-1", "text": "hmm"})
    assert out == {"type": "reasoning", "text": "hmm"}


def test_normalize_tool_start():
    ev = {
        "type": "tool.start",
        "turnId": "t-1",
        "toolCall": {"id": "tc-1", "name": "bash", "kind": "tool",
                     "summary": "sum", "reasoningWhy": "why", "args": {"cmd": "ls"}},
    }
    out = _normalize_ws_event(ev)
    assert out == {
        "type": "tool.start",
        "tool_id": "tc-1",
        "name": "bash",
        "kind": "tool",
        "summary": "sum",
        "reasoning_why": "why",
        "args": {"cmd": "ls"},
    }


def test_normalize_tool_done():
    ev = {"type": "tool.done", "turnId": "t-1", "toolId": "tc-1",
          "status": "done", "summary": "ok", "result": None, "durationMs": 123}
    out = _normalize_ws_event(ev)
    assert out == {
        "type": "tool.done",
        "tool_id": "tc-1",
        "status": "done",
        "summary": "ok",
        "result": None,
        "duration_ms": 123,
    }


def test_normalize_tool_request():
    ev = {"type": "tool.request", "turnId": "t-1",
          "toolRequestId": "req-1", "name": "read_file", "args": {"path": "a.txt"}}
    out = _normalize_ws_event(ev)
    assert out == {
        "type": "tool.request",
        "tool_request_id": "req-1",
        "name": "read_file",
        "args": {"path": "a.txt"},
    }


def test_normalize_passthrough():
    ev = {"type": "answer.delta", "turnId": "t-1", "text": "hi"}
    assert _normalize_ws_event(ev) == ev


# ── WSEventStream ──────────────────────────────────────────────────────────────

def _make_ws(*events: dict):
    """Fake websocket that serves events as JSON strings then raises StopAsyncIteration."""
    frames = [json.dumps(e) for e in events]
    ws = MagicMock()
    ws.recv = AsyncMock(side_effect=frames + [Exception("closed")])
    ws.send = AsyncMock()
    return ws


@pytest.mark.asyncio
async def test_ws_event_stream_first_returns_first_event():
    ws = _make_ws(
        {"type": "turn.created"},
        {"type": "node.enter", "turnId": "t-1", "node": "plan", "thinkingBudget": 0},
        {"type": "turn.done", "turnId": "t-1", "status": "complete"},
    )
    stream = WSEventStream(ws)
    first = await stream.first(timeout=1.0)
    assert first is not None
    assert first["type"] == "turn.created"


@pytest.mark.asyncio
async def test_ws_event_stream_events_normalises_and_stops_at_turn_done():
    ws = _make_ws(
        {"type": "node.enter", "turnId": "t-1", "node": "plan", "thinkingBudget": 0},
        {"type": "answer.delta", "turnId": "t-1", "text": "hello"},
        {"type": "turn.done", "turnId": "t-1", "status": "complete"},
        {"type": "answer.delta", "turnId": "t-1", "text": "AFTER"},  # must not appear
    )
    stream = WSEventStream(ws)
    _ = await stream.first(timeout=1.0)
    seen = [ev async for ev in stream.events()]
    assert [e["type"] for e in seen] == ["turn.start", "answer.delta", "turn.done"]
    assert seen[1]["text"] == "hello"


@pytest.mark.asyncio
async def test_ws_event_stream_result_synthesises_answer():
    ws = _make_ws(
        {"type": "node.enter", "turnId": "t-1", "node": "plan", "thinkingBudget": 0},
        {"type": "answer.delta", "turnId": "t-1", "text": "hello "},
        {"type": "answer.delta", "turnId": "t-1", "text": "world"},
        {"type": "turn.done", "turnId": "t-1", "status": "complete"},
    )
    stream = WSEventStream(ws)
    _ = await stream.first(timeout=1.0)
    async for _ in stream.events():
        pass
    result = stream.result()
    assert result["ok"] is True
    assert result["state"]["final_answer"] == "hello world"


@pytest.mark.asyncio
async def test_ws_event_stream_stops_on_closed_connection():
    ws = _make_ws(
        {"type": "answer.delta", "turnId": "t-1", "text": "partial"},
    )
    stream = WSEventStream(ws)
    _ = await stream.first(timeout=1.0)
    seen = [ev async for ev in stream.events()]
    # After Exception("closed"), stream stops — answer.delta was already consumed by first()
    # so events() starts from the pending buffer
    assert seen[0]["type"] == "answer.delta"


# ── LabmateWSClient ────────────────────────────────────────────────────────────

def _make_client_ws(*events: dict):
    """Fake WS that serves auth handshake then events."""
    # First recv returns auth.ok, subsequent calls return the events
    frames = [json.dumps({"type": "auth.ok", "user": {"id": "u-1", "email": "a@b.com", "role": "user"}})]
    frames += [json.dumps(e) for e in events]
    ws = MagicMock()
    ws.recv = AsyncMock(side_effect=frames + [Exception("closed")])
    ws.send = AsyncMock()
    ws.close = AsyncMock()
    ws.__aenter__ = AsyncMock(return_value=ws)
    ws.__aexit__ = AsyncMock(return_value=False)
    return ws


@pytest.mark.asyncio
async def test_client_push_task_sends_send_frame():
    ws = _make_client_ws(
        {"type": "turn.created", "turn": {"id": "turn-1"}},
        {"type": "turn.done", "turnId": "turn-1", "status": "complete"},
    )
    with patch("services.cli.ws_client.websockets.connect", new=AsyncMock(return_value=ws)):
        client = LabmateWSClient("ws://localhost:8787/ws", "tok")
        await client.connect()
        await client.push_task("task-1", "do the thing", "session-1")
        sent = [json.loads(c.args[0]) for c in ws.send.call_args_list]
        assert any(f.get("type") == "send" and f.get("text") == "do the thing" for f in sent)


@pytest.mark.asyncio
async def test_client_send_tool_result_sends_tool_result_frame():
    ws = _make_client_ws()
    with patch("services.cli.ws_client.websockets.connect", new=AsyncMock(return_value=ws)):
        client = LabmateWSClient("ws://localhost:8787/ws", "tok")
        await client.connect()
        await client.send_tool_result("req-1", {"content": "hi"}, None)
        sent = [json.loads(c.args[0]) for c in ws.send.call_args_list]
        assert any(
            f.get("type") == "tool.result"
            and f.get("toolRequestId") == "req-1"
            and f.get("result") == {"content": "hi"}
            for f in sent
        )


@pytest.mark.asyncio
async def test_client_get_result_consumes_events_and_returns_answer():
    ws = _make_client_ws(
        {"type": "turn.created"},
        {"type": "answer.delta", "turnId": "t-1", "text": "done"},
        {"type": "turn.done", "turnId": "t-1", "status": "complete"},
    )
    with patch("services.cli.ws_client.websockets.connect", new=AsyncMock(return_value=ws)):
        client = LabmateWSClient("ws://localhost:8787/ws", "tok")
        await client.connect()
        await client.push_task("task-1", "go", "s-1")
        result = await client.get_result("task-1", timeout=5.0)
    assert result["ok"] is True
    assert result["state"]["final_answer"] == "done"


@pytest.mark.asyncio
async def test_ws_event_stream_closed_connection_yields_exactly_one_event():
    ws = _make_ws(
        {"type": "answer.delta", "turnId": "t-1", "text": "partial"},
    )
    stream = WSEventStream(ws)
    _ = await stream.first(timeout=1.0)
    seen = [ev async for ev in stream.events()]
    assert len(seen) == 1
    assert seen[0]["type"] == "answer.delta"


@pytest.mark.asyncio
async def test_ws_event_stream_skips_malformed_json_frame():
    frames = [
        json.dumps({"type": "node.enter", "turnId": "t-1", "node": "plan", "thinkingBudget": 0}),
        "NOT VALID JSON",
        json.dumps({"type": "turn.done", "turnId": "t-1", "status": "complete"}),
    ]
    ws = MagicMock()
    ws.recv = AsyncMock(side_effect=frames + [Exception("closed")])
    stream = WSEventStream(ws)
    _ = await stream.first(timeout=1.0)
    seen = [ev async for ev in stream.events()]
    types = [e["type"] for e in seen]
    assert "turn.done" in types
    assert "turn.start" in types
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_client_connect_raises_on_auth_error():
    ws = MagicMock()
    ws.send = AsyncMock()
    ws.recv = AsyncMock(return_value=json.dumps({"type": "auth.error", "reason": "bad token"}))
    ws.close = AsyncMock()
    with patch("services.cli.ws_client.websockets.connect", new=AsyncMock(return_value=ws)):
        client = LabmateWSClient("ws://localhost:8787/ws", "bad-tok")
        with pytest.raises(PermissionError, match="bad token"):
            await client.connect()


@pytest.mark.asyncio
async def test_client_get_result_returns_partial_when_stream_closes_without_turn_done():
    """Stream closed by Exception without turn.done → result synthesises partial answer."""
    ws = _make_client_ws(
        {"type": "answer.delta", "turnId": "t-1", "text": "partial answer"},
        # No turn.done — stream ends when Exception("closed") fires
    )
    with patch("services.cli.ws_client.websockets.connect", new=AsyncMock(return_value=ws)):
        client = LabmateWSClient("ws://localhost:8787/ws", "tok")
        await client.connect()
        await client.push_task("t-1", "go", "s-1")
        result = await client.get_result("t-1", timeout=5.0)
    assert result["ok"] is True
    assert result["state"]["final_answer"] == "partial answer"
