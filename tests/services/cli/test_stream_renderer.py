from __future__ import annotations

import pytest
from rich.console import Console


def _plain(renderable) -> str:
    console = Console(width=100, no_color=True, highlight=False)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


def test_turn_start_sets_active_indicator():
    from services.cli.stream_renderer import StreamRenderer
    r = StreamRenderer()
    r.handle({"type": "turn.start", "task": "do the thing"})
    out = _plain(r.render())
    assert "◆" in out


def test_tool_start_then_done_updates_row():
    from services.cli.stream_renderer import StreamRenderer
    r = StreamRenderer()
    r.handle({"type": "tool.start", "tool_id": "tc1", "name": "exec_run",
              "kind": "tool", "args": {}, "reasoning_why": "needs shell"})
    out = _plain(r.render())
    assert "exec_run" in out and "⚙" in out

    r.handle({"type": "tool.done", "tool_id": "tc1", "status": "done",
              "summary": "exit 0", "result": {}, "duration_ms": 1200})
    out = _plain(r.render())
    assert "✓" in out and "exit 0" in out and "1.2s" in out


def test_tool_error_shows_cross():
    from services.cli.stream_renderer import StreamRenderer
    r = StreamRenderer()
    r.handle({"type": "tool.start", "tool_id": "tc2", "name": "exec_run",
              "kind": "tool", "args": {}, "reasoning_why": ""})
    r.handle({"type": "tool.done", "tool_id": "tc2", "status": "error",
              "summary": "boom", "result": {}, "duration_ms": 300})
    out = _plain(r.render())
    assert "✗" in out and "boom" in out


def test_reasoning_accumulates():
    from services.cli.stream_renderer import StreamRenderer
    r = StreamRenderer()
    r.handle({"type": "reasoning", "node": "route", "summary": "why", "text": "thinking deeply"})
    assert r.reasoning_text == "thinking deeply"
    out = _plain(r.render())
    assert "thinking deeply" in out


def test_multiple_reasoning_events_append():
    from services.cli.stream_renderer import StreamRenderer
    r = StreamRenderer()
    r.handle({"type": "reasoning", "node": "plan", "summary": "a", "text": "step one"})
    r.handle({"type": "reasoning", "node": "execute", "summary": "b", "text": "step two"})
    assert "step one" in r.reasoning_text and "step two" in r.reasoning_text


def test_answer_delta_accumulates():
    from services.cli.stream_renderer import StreamRenderer
    r = StreamRenderer()
    r.handle({"type": "answer.delta", "text": "Hello "})
    r.handle({"type": "answer.delta", "text": "world"})
    assert r.answer_text == "Hello world"
    out = _plain(r.render())
    assert "Hello world" in out


def test_answer_done_overwrites_accumulated():
    from services.cli.stream_renderer import StreamRenderer
    r = StreamRenderer()
    r.handle({"type": "answer.delta", "text": "partial"})
    r.handle({"type": "answer.done", "text": "complete final answer"})
    assert r.answer_text == "complete final answer"


def test_turn_done_marks_complete_and_hides_active_indicator():
    from services.cli.stream_renderer import StreamRenderer
    r = StreamRenderer()
    r.handle({"type": "turn.start", "task": "hi"})
    r.handle({"type": "turn.done", "status": "complete", "final_answer": "done"})
    assert r.done is True
    assert r.status == "complete"
    out = _plain(r.render())
    assert "◆" not in out  # active indicator hidden after done


def test_unknown_event_is_ignored():
    from services.cli.stream_renderer import StreamRenderer
    r = StreamRenderer()
    r.handle({"type": "node.enter", "node": "plan_node"})    # old format — dropped
    r.handle({"type": "context.update", "window": {}})        # not emitted — dropped
    r.handle({"type": "agent.status", "status": {}})          # not emitted — dropped
    assert r.answer_text == "" and r.reasoning_text == ""
