# CLI Streaming Renderer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Labmate CLI subscribe to `labmate:events:<task_id>` and render `StreamEvent`s (FRONTEND_SPEC.md §4) live with Rich's `Live` display — node status lines, tool start/done rows, dim-italic streamed reasoning, streamed answer — while falling back to the existing `get_result()` final-answer path when no events arrive.

**Architecture:** A new `EventStream` (in `services/cli/event_stream.py`) subscribes to the per-task event channel and yields parsed event dicts. A new `StreamRenderer` (in `services/cli/stream_renderer.py`) consumes those events into a Rich `Live`-driven view. `repl.py` and `main.py` race the event stream against `get_result()`: if the first event arrives before a timeout, stream live and then read the final result for the canonical answer; if it times out, render exactly as today. No orchestrator changes — this plan is independent of Plan 1 (with Plan 1 absent, no events arrive and the fallback runs).

**Tech Stack:** Python, `rich` (`Live`, `Group`, `Text`, `Markdown`, `Spinner`), `redis.asyncio` (pub/sub subscribe), `asyncio` (race first-event vs timeout), existing `LabmateRedisClient.get_result()` as fallback.

---

## Event contract consumed (FRONTEND_SPEC.md §4 — must match exactly)

The renderer consumes JSON objects published by the orchestrator (Plan 1).
Every event has the envelope `{ type, sessionId, turnId, seq }` plus
type-specific fields. The CLI reads only these variants (others are ignored
harmlessly):

```ts
| { type: 'node.enter'; turnId; node: NodeName; thinkingBudget: number }
| { type: 'reasoning.delta'; turnId; text: string }
| { type: 'answer.delta'; turnId; text: string }
| { type: 'tool.start'; turnId; toolCall: { id; name; kind; summary; reasoningWhy; args } }
| { type: 'tool.done'; turnId; toolId; status; summary; result; durationMs }
| { type: 'turn.done'; turnId; status: 'complete' | 'error' }
| { type: 'context.update'; window: ContextWindow }   // consumed for % readout
| { type: 'agent.status'; status: AgentStatus }        // consumed for spinner state
```

`NodeName = 'plan_node' | 'execute_node' | 'check_node' | 'reflect_node' | 'chat_node'`.
The renderer never invents or requires variants outside this set; unknown
`type` values are dropped.

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `services/cli/event_stream.py` | Create | `EventStream` — subscribe to `labmate:events:<task_id>`, yield parsed events, race-safe first-event detection |
| `services/cli/stream_renderer.py` | Create | `StreamRenderer` — reduce events into a Rich `Live` view (node lines, tool rows, reasoning block, answer) |
| `services/cli/redis_client.py` | Modify | Add `subscribe_events()` returning an `EventStream`; `get_result()` unchanged (fallback) |
| `services/cli/repl.py` | Modify | `_send_task()` races live stream vs `get_result()`; live on first event, fallback otherwise |
| `services/cli/main.py` | Modify | One-shot path mirrors the REPL race |
| `tests/services/cli/test_event_stream.py` | Create | `EventStream` parse/subscribe/timeout tests (mock Redis pubsub) |
| `tests/services/cli/test_stream_renderer.py` | Create | `StreamRenderer` reduction + render tests (no live terminal) |
| `tests/services/cli/test_repl_streaming.py` | Create | `_send_task()` race + fallback tests |

---

### Task 1: Event channel constant + module scaffold

**Files:**
- Create: `services/cli/event_stream.py`
- Create: `tests/services/cli/test_event_stream.py`

- [ ] **Step 1: Write failing test** — `tests/services/cli/test_event_stream.py`

```python
from __future__ import annotations

import pytest


def test_event_channel_constant_and_helper():
    from services.cli.event_stream import EVENTS_PREFIX, event_channel

    assert EVENTS_PREFIX == "labmate:events:"
    assert event_channel("t-1") == "labmate:events:t-1"
```

- [ ] **Step 2: Run it (must fail)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/cli/test_event_stream.py -q
```

Expected: `ModuleNotFoundError: No module named 'services.cli.event_stream'`.

- [ ] **Step 3: Implement** — `services/cli/event_stream.py`

```python
# services/cli/event_stream.py
"""
Client-side subscriber for the orchestrator's live StreamEvent channel
(labmate:events:<task_id>, FRONTEND_SPEC §4). Independent of the orchestrator:
if nothing publishes, the stream simply yields nothing and the caller falls
back to get_result().
"""
from __future__ import annotations

EVENTS_PREFIX = "labmate:events:"


def event_channel(task_id: str) -> str:
    return f"{EVENTS_PREFIX}{task_id}"
```

- [ ] **Step 4: Run tests (must pass)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/cli/test_event_stream.py -q
```

Expected: `1 passed`.

- [ ] **Step 5: Commit**

```bash
git add services/cli/event_stream.py tests/services/cli/test_event_stream.py
git commit -m "feat(cli): event channel constant for live streaming"
```

---

### Task 2: EventStream — subscribe, parse, timeout-aware iteration

**Files:**
- Modify: `services/cli/event_stream.py`
- Modify: `tests/services/cli/test_event_stream.py`

`EventStream` wraps a Redis pubsub object. It exposes:
- `await subscribe()` — subscribe to the channel.
- `await first(timeout)` — return the first parsed event dict, or `None` if no
  message arrives within `timeout` (this is how the caller decides live vs
  fallback).
- `async for ev in stream.events()` — yield parsed events until a `turn.done`
  is seen (or the pubsub closes). `first()`'s event, if any, is replayed as the
  first item so no event is lost.
- `await aclose()` — unsubscribe + close.

Malformed JSON messages are skipped (never raise).

- [ ] **Step 1: Write failing test** — append to `tests/services/cli/test_event_stream.py`

```python
import json
from unittest.mock import AsyncMock, MagicMock


def _msg(payload: dict | bytes):
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return {"type": "message", "data": data}


def _make_pubsub(messages):
    """A pubsub whose get_message yields each item then None forever."""
    pubsub = AsyncMock(name="pubsub")
    seq = list(messages)

    async def get_message(ignore_subscribe_messages=True, timeout=0.0):
        if seq:
            return seq.pop(0)
        return None

    pubsub.get_message = get_message
    pubsub.subscribe = AsyncMock()
    pubsub.unsubscribe = AsyncMock()
    pubsub.aclose = AsyncMock()
    return pubsub


def _stream(messages):
    from services.cli.event_stream import EventStream
    redis = MagicMock()
    pubsub = _make_pubsub(messages)
    redis.pubsub = MagicMock(return_value=pubsub)
    return EventStream(redis, "t-1"), pubsub


async def test_subscribe_calls_redis_subscribe():
    stream, pubsub = _stream([])
    await stream.subscribe()
    pubsub.subscribe.assert_awaited_once_with("labmate:events:t-1")


async def test_first_returns_parsed_event():
    stream, _ = _stream([_msg({"type": "node.enter", "node": "plan_node", "seq": 0})])
    await stream.subscribe()
    ev = await stream.first(timeout=1.0)
    assert ev["type"] == "node.enter"
    assert ev["node"] == "plan_node"


async def test_first_returns_none_on_timeout():
    stream, _ = _stream([])  # no messages ever
    await stream.subscribe()
    ev = await stream.first(timeout=0.05)
    assert ev is None


async def test_events_replays_first_then_continues_until_turn_done():
    msgs = [
        _msg({"type": "node.enter", "node": "plan_node", "seq": 0}),
        _msg({"type": "answer.delta", "text": "hi", "seq": 1}),
        _msg({"type": "turn.done", "status": "complete", "seq": 2}),
        _msg({"type": "answer.delta", "text": "AFTER", "seq": 3}),  # must NOT appear
    ]
    stream, _ = _stream(msgs)
    await stream.subscribe()
    first = await stream.first(timeout=1.0)
    assert first["type"] == "node.enter"

    seen = [ev async for ev in stream.events()]
    types = [e["type"] for e in seen]
    # first event replayed, stops at turn.done, drops post-turn events
    assert types == ["node.enter", "answer.delta", "turn.done"]


async def test_malformed_json_is_skipped():
    msgs = [
        _msg(b"not-json"),
        _msg({"type": "turn.done", "status": "complete", "seq": 0}),
    ]
    stream, _ = _stream(msgs)
    await stream.subscribe()
    seen = [ev async for ev in stream.events()]
    assert [e["type"] for e in seen] == ["turn.done"]


async def test_aclose_unsubscribes_and_closes():
    stream, pubsub = _stream([])
    await stream.subscribe()
    await stream.aclose()
    pubsub.unsubscribe.assert_awaited_once_with("labmate:events:t-1")
    pubsub.aclose.assert_awaited_once()
```

- [ ] **Step 2: Run it (must fail)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/cli/test_event_stream.py -q
```

Expected: `ImportError: cannot import name 'EventStream'`.

- [ ] **Step 3: Implement** — append to `services/cli/event_stream.py`

```python
import asyncio
import json
import time
from typing import AsyncIterator, Optional


class EventStream:
    """Subscribe to labmate:events:<task_id> and yield parsed StreamEvents.

    Usage:
        stream = EventStream(redis, task_id)
        await stream.subscribe()
        first = await stream.first(timeout=2.0)   # None -> fall back
        if first is not None:
            async for ev in stream.events():      # replays `first`, then more
                ...
        await stream.aclose()
    """

    def __init__(self, redis, task_id: str) -> None:
        self._channel = event_channel(task_id)
        self._pubsub = redis.pubsub()
        self._buffered: Optional[dict] = None
        self._done = False

    async def subscribe(self) -> None:
        await self._pubsub.subscribe(self._channel)

    @staticmethod
    def _parse(msg) -> Optional[dict]:
        if not msg or msg.get("type") != "message":
            return None
        data = msg.get("data")
        if isinstance(data, (bytes, bytearray)):
            data = data.decode("utf-8", "replace")
        if not isinstance(data, str):
            return None
        try:
            obj = json.loads(data)
        except (ValueError, TypeError):
            return None
        return obj if isinstance(obj, dict) else None

    async def first(self, timeout: float) -> Optional[dict]:
        """Return the first parsed event within `timeout`, else None.

        A returned event is buffered and replayed as the first item of
        events(), so it is never lost.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = await self._pubsub.get_message(
                ignore_subscribe_messages=True, timeout=0.1,
            )
            ev = self._parse(msg)
            if ev is not None:
                self._buffered = ev
                return ev
            await asyncio.sleep(0.01)
        return None

    async def events(self) -> AsyncIterator[dict]:
        """Yield events until (and including) turn.done, then stop."""
        if self._buffered is not None:
            ev = self._buffered
            self._buffered = None
            yield ev
            if ev.get("type") == "turn.done":
                self._done = True
                return
        while not self._done:
            msg = await self._pubsub.get_message(
                ignore_subscribe_messages=True, timeout=0.5,
            )
            ev = self._parse(msg)
            if ev is None:
                await asyncio.sleep(0.01)
                continue
            yield ev
            if ev.get("type") == "turn.done":
                self._done = True
                return

    async def aclose(self) -> None:
        try:
            await self._pubsub.unsubscribe(self._channel)
        finally:
            await self._pubsub.aclose()
```

> The test's fake `get_message` returns `None` forever after draining, which
> would make `events()` loop until `turn.done`. Each test stream ends with a
> `turn.done`, so iteration terminates. The `timeout` test relies on
> `first()`'s monotonic deadline, not on the fake.

- [ ] **Step 4: Run tests (must pass)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/cli/test_event_stream.py -q
```

Expected: `7 passed`.

- [ ] **Step 5: Commit**

```bash
git add services/cli/event_stream.py tests/services/cli/test_event_stream.py
git commit -m "feat(cli): EventStream subscribe/parse/first/events with turn.done termination"
```

---

### Task 3: StreamRenderer — reduce events into a Rich renderable

**Files:**
- Create: `services/cli/stream_renderer.py`
- Create: `tests/services/cli/test_stream_renderer.py`

`StreamRenderer` is a pure reducer: `handle(event)` mutates internal view state,
`render()` returns a Rich renderable (a `Group`) representing the current frame.
This separation lets us unit-test the reduction without a live terminal. Glyphs
match the prompt's Claude-Code feel:
- node line: `◆ {node}  thinking…`
- tool running: `⚙ {name}  {summary}`
- tool done: `✓ {name}  {summary}  ({sec}s)` (or `✗` on error)
- reasoning: dim italic, collapsible (we show last N lines while streaming)
- answer: streamed markdown text

- [ ] **Step 1: Write failing test** — `tests/services/cli/test_stream_renderer.py`

```python
from __future__ import annotations

import pytest
from rich.console import Console


def _plain(renderable) -> str:
    """Render to plain text for assertions (no ANSI, fixed width)."""
    console = Console(width=100, no_color=True, highlight=False)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


def test_node_enter_shows_status_line():
    from services.cli.stream_renderer import StreamRenderer
    r = StreamRenderer()
    r.handle({"type": "node.enter", "node": "plan_node", "thinkingBudget": 3000})
    out = _plain(r.render())
    assert "plan_node" in out
    assert "◆" in out


def test_tool_start_then_done_updates_row():
    from services.cli.stream_renderer import StreamRenderer
    r = StreamRenderer()
    r.handle({"type": "tool.start", "toolCall": {
        "id": "tc1", "name": "exec_run", "kind": "tool",
        "summary": "Running bash…", "reasoningWhy": "", "args": {}}})
    out = _plain(r.render())
    assert "exec_run" in out and "Running" in out and "⚙" in out

    r.handle({"type": "tool.done", "toolId": "tc1", "status": "done",
              "summary": "exit 0", "result": {}, "durationMs": 1200})
    out = _plain(r.render())
    assert "✓" in out and "exit 0" in out and "1.2s" in out


def test_tool_error_shows_cross():
    from services.cli.stream_renderer import StreamRenderer
    r = StreamRenderer()
    r.handle({"type": "tool.start", "toolCall": {
        "id": "tc2", "name": "exec_run", "kind": "tool",
        "summary": "Running…", "reasoningWhy": "", "args": {}}})
    r.handle({"type": "tool.done", "toolId": "tc2", "status": "error",
              "summary": "boom", "result": {}, "durationMs": 300})
    out = _plain(r.render())
    assert "✗" in out and "boom" in out


def test_reasoning_delta_accumulates_dim():
    from services.cli.stream_renderer import StreamRenderer
    r = StreamRenderer()
    r.handle({"type": "reasoning.delta", "text": "first "})
    r.handle({"type": "reasoning.delta", "text": "second"})
    assert r.reasoning_text == "first second"
    out = _plain(r.render())
    assert "first second" in out


def test_answer_delta_accumulates():
    from services.cli.stream_renderer import StreamRenderer
    r = StreamRenderer()
    r.handle({"type": "answer.delta", "text": "Hello "})
    r.handle({"type": "answer.delta", "text": "world"})
    assert r.answer_text == "Hello world"
    out = _plain(r.render())
    assert "Hello world" in out


def test_context_update_sets_percent():
    from services.cli.stream_renderer import StreamRenderer
    r = StreamRenderer()
    r.handle({"type": "context.update", "window": {
        "max": 16384, "used": 1638, "segments": {}, "free": 14746}})
    assert r.context_pct == 10  # round(1638/16384*100)


def test_agent_status_tracks_state_and_node():
    from services.cli.stream_renderer import StreamRenderer
    r = StreamRenderer()
    r.handle({"type": "agent.status", "status": {"brain": {
        "state": "active", "node": "execute_node", "model": "m",
        "endpoint": "e", "thinkingBudget": 2048}}})
    assert r.brain_state == "active"
    assert r.current_node == "execute_node"


def test_turn_done_marks_complete():
    from services.cli.stream_renderer import StreamRenderer
    r = StreamRenderer()
    r.handle({"type": "turn.done", "status": "complete"})
    assert r.done is True
    assert r.status == "complete"


def test_unknown_event_is_ignored():
    from services.cli.stream_renderer import StreamRenderer
    r = StreamRenderer()
    r.handle({"type": "session.updated", "session": {}})  # not consumed by CLI
    r.handle({"type": "turn.created", "turn": {}})
    assert r.answer_text == "" and r.reasoning_text == ""
```

- [ ] **Step 2: Run it (must fail)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/cli/test_stream_renderer.py -q
```

Expected: `ModuleNotFoundError: No module named 'services.cli.stream_renderer'`.

- [ ] **Step 3: Implement** — `services/cli/stream_renderer.py`

```python
# services/cli/stream_renderer.py
"""
Reduce orchestrator StreamEvents (FRONTEND_SPEC §4) into a live Rich view.

Pure reducer: handle(event) mutates state; render() returns a Rich renderable.
The Live loop (driver.py / repl.py) calls handle() then render() per event.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from rich.console import Group
from rich.markdown import Markdown
from rich.text import Text


@dataclass
class _ToolRow:
    name: str
    summary: str
    status: str = "running"   # running | done | error
    duration_ms: int = 0


class StreamRenderer:
    """Accumulates events into a renderable frame for rich.live.Live."""

    def __init__(self) -> None:
        self.current_node: str | None = None
        self.brain_state: str = "idle"
        self.reasoning_text: str = ""
        self.answer_text: str = ""
        self.context_pct: int = 0
        self.done: bool = False
        self.status: str = ""
        self._tools: dict[str, _ToolRow] = {}
        self._tool_order: list[str] = []

    # --- reduction --------------------------------------------------------

    def handle(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "node.enter":
            self.current_node = event.get("node")
        elif etype == "agent.status":
            brain = (event.get("status") or {}).get("brain") or {}
            self.brain_state = brain.get("state", self.brain_state)
            if brain.get("node"):
                self.current_node = brain["node"]
        elif etype == "reasoning.delta":
            self.reasoning_text += event.get("text", "")
        elif etype == "answer.delta":
            self.answer_text += event.get("text", "")
        elif etype == "tool.start":
            tc = event.get("toolCall") or {}
            tid = tc.get("id", "")
            self._tools[tid] = _ToolRow(
                name=tc.get("name", "tool"), summary=tc.get("summary", ""),
            )
            self._tool_order.append(tid)
        elif etype == "tool.done":
            tid = event.get("toolId", "")
            row = self._tools.get(tid)
            if row is not None:
                row.status = event.get("status", "done")
                row.summary = event.get("summary", row.summary)
                row.duration_ms = event.get("durationMs", 0)
        elif etype == "context.update":
            w = event.get("window") or {}
            mx = w.get("max", 0) or 1
            self.context_pct = round((w.get("used", 0) / mx) * 100)
        elif etype == "turn.done":
            self.done = True
            self.status = event.get("status", "complete")
        # unknown types: ignored

    # --- rendering --------------------------------------------------------

    def _tool_line(self, row: _ToolRow) -> Text:
        if row.status == "running":
            return Text(f"⚙ {row.name}  {row.summary}", style="yellow")
        secs = f"{row.duration_ms / 1000:.1f}s"
        if row.status == "error":
            return Text(f"✗ {row.name}  {row.summary}  ({secs})", style="red")
        return Text(f"✓ {row.name}  {row.summary}  ({secs})", style="green")

    def render(self):
        parts: list = []

        if self.current_node and not self.done:
            parts.append(Text(f"◆ {self.current_node}  thinking…", style="cyan"))

        for tid in self._tool_order:
            parts.append(self._tool_line(self._tools[tid]))

        if self.reasoning_text:
            parts.append(Text(self.reasoning_text, style="dim italic"))

        if self.answer_text:
            parts.append(Markdown(self.answer_text))

        if self.context_pct and not self.done:
            parts.append(Text(f"context {self.context_pct}%", style="dim"))

        if not parts:
            parts.append(Text("working…", style="dim"))

        return Group(*parts)
```

- [ ] **Step 4: Run tests (must pass)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/cli/test_stream_renderer.py -q
```

Expected: `10 passed`.

- [ ] **Step 5: Commit**

```bash
git add services/cli/stream_renderer.py tests/services/cli/test_stream_renderer.py
git commit -m "feat(cli): StreamRenderer reduces StreamEvents into a Rich frame"
```

---

### Task 4: redis_client.subscribe_events()

**Files:**
- Modify: `services/cli/redis_client.py`
- Modify: `tests/services/cli/test_redis_client.py`

Expose an `EventStream` from `LabmateRedisClient` so callers don't reach into
the raw redis object. `get_result()` is untouched — it remains the fallback.

- [ ] **Step 1: Write failing test** — append to `tests/services/cli/test_redis_client.py`

```python
@pytest.mark.asyncio
async def test_subscribe_events_returns_event_stream(mock_redis):
    from services.cli.event_stream import EventStream
    pubsub = AsyncMock()
    pubsub.subscribe = AsyncMock()
    mock_redis.pubsub = MagicMock(return_value=pubsub)

    client = LabmateRedisClient.__new__(LabmateRedisClient)
    client._redis = mock_redis

    stream = client.subscribe_events("t-99")
    assert isinstance(stream, EventStream)
    await stream.subscribe()
    pubsub.subscribe.assert_awaited_once_with("labmate:events:t-99")
```

- [ ] **Step 2: Run it (must fail)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/cli/test_redis_client.py::test_subscribe_events_returns_event_stream -q
```

Expected: `AttributeError: 'LabmateRedisClient' object has no attribute 'subscribe_events'`.

- [ ] **Step 3: Implement** — edit `services/cli/redis_client.py`

Add import near the top:
```python
from .event_stream import EventStream
```

Add this method to `LabmateRedisClient` (after `get_result`):
```python
    def subscribe_events(self, task_id: str) -> EventStream:
        """Return an EventStream for labmate:events:<task_id>.

        Caller must `await stream.subscribe()` before reading. Independent of
        the result path — if the orchestrator publishes no events, the stream
        yields nothing and the caller falls back to get_result().
        """
        return EventStream(self._redis, task_id)
```

- [ ] **Step 4: Run tests (must pass)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/cli/test_redis_client.py -q
```

Expected: all pass (existing `get_result`/`push_task` tests unchanged).

- [ ] **Step 5: Commit**

```bash
git add services/cli/redis_client.py tests/services/cli/test_redis_client.py
git commit -m "feat(cli): LabmateRedisClient.subscribe_events() returns EventStream"
```

---

### Task 5: Renderer.stream_live() — drive Live from an EventStream

**Files:**
- Modify: `services/cli/renderer.py`
- Modify: `tests/services/cli/test_renderer.py`

Add a `stream_live()` coroutine to the existing `Renderer` that owns the Rich
`Live` loop: it consumes events from an `EventStream`, feeds each into a
`StreamRenderer`, refreshes the `Live` frame, and returns the final
`StreamRenderer` (so the caller can read `.answer_text` / `.status`). It does
**not** subscribe or close the stream — the caller owns that lifecycle.

- [ ] **Step 1: Write failing test** — append to `tests/services/cli/test_renderer.py`

```python
import pytest


class _FakeStream:
    def __init__(self, events):
        self._events = events

    async def events(self):
        for e in self._events:
            yield e


@pytest.mark.asyncio
async def test_stream_live_consumes_events_and_returns_renderer():
    from services.cli.renderer import Renderer
    r = Renderer()
    fake = _FakeStream([
        {"type": "node.enter", "node": "plan_node", "thinkingBudget": 3000},
        {"type": "answer.delta", "text": "Hello "},
        {"type": "answer.delta", "text": "world"},
        {"type": "turn.done", "status": "complete"},
    ])
    sr = await r.stream_live(fake)
    assert sr.answer_text == "Hello world"
    assert sr.status == "complete"
    assert sr.done is True


@pytest.mark.asyncio
async def test_stream_live_handles_empty_stream():
    from services.cli.renderer import Renderer
    r = Renderer()
    sr = await r.stream_live(_FakeStream([]))
    assert sr.answer_text == ""
    assert sr.done is False
```

- [ ] **Step 2: Run it (must fail)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/cli/test_renderer.py -q
```

Expected: `AttributeError: 'Renderer' object has no attribute 'stream_live'`.

- [ ] **Step 3: Implement** — edit `services/cli/renderer.py`

Add the import at the top:
```python
from .stream_renderer import StreamRenderer
```

Add this method to `Renderer`:
```python
    async def stream_live(self, stream) -> StreamRenderer:
        """Drive a rich.live.Live frame from an EventStream.

        Consumes stream.events() into a StreamRenderer, refreshing the Live
        view per event. Returns the StreamRenderer so the caller can read the
        accumulated answer/status. Does not subscribe or close `stream`.
        """
        sr = StreamRenderer()
        with Live(
            sr.render(),
            console=self._console,
            refresh_per_second=12,
            transient=False,
        ) as live:
            async for event in stream.events():
                sr.handle(event)
                live.update(sr.render())
        return sr
```

- [ ] **Step 4: Run tests (must pass)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/cli/test_renderer.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add services/cli/renderer.py tests/services/cli/test_renderer.py
git commit -m "feat(cli): Renderer.stream_live drives Rich Live from EventStream"
```

---

### Task 6: REPL._send_task() — race live stream vs get_result() fallback

**Files:**
- Modify: `services/cli/repl.py`
- Create: `tests/services/cli/test_repl_streaming.py`

`_send_task()` becomes: push the task, subscribe to events, await
`stream.first(timeout=FIRST_EVENT_TIMEOUT)`. If an event arrives → `stream_live`
the rest, then read the canonical answer via `get_result()` (fast path — it's
already set by the time `turn.done` fires) and print it cleanly. If no event
arrives within the timeout → fall back to today's `thinking()` + `get_result()`
path verbatim. Streaming never blocks the canonical answer; the live view is
additive.

- [ ] **Step 1: Write failing test** — `tests/services/cli/test_repl_streaming.py`

```python
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from services.cli.repl import REPL, REPLContext, FIRST_EVENT_TIMEOUT
from services.cli.identity import Identity


def _ctx():
    return REPLContext(
        identity=Identity(user_id="u-1", display_name="Tester"),
        workspace_id="ws-1", workspace_name="WS", workspace_paths=[],
        workspace_instructions=None, session_id="s-1",
        redis_url="redis://localhost:6379/0",
    )


def _repl_with_mocks(first_event, stream_events, result):
    r = REPL.__new__(REPL)
    r._ctx = _ctx()
    r._renderer = MagicMock()
    r._renderer.stream_live = AsyncMock()
    r._renderer.print_answer = MagicMock()
    r._renderer.print_error = MagicMock()
    r._renderer.thinking = MagicMock()
    r._sessions = MagicMock()
    r._sessions.append = MagicMock()

    redis = MagicMock()
    redis.push_task = AsyncMock()
    redis.get_result = AsyncMock(return_value=result)

    stream = AsyncMock()
    stream.subscribe = AsyncMock()
    stream.first = AsyncMock(return_value=first_event)
    stream.aclose = AsyncMock()

    async def _events():
        for e in stream_events:
            yield e
    stream.events = _events

    redis.subscribe_events = MagicMock(return_value=stream)
    r._redis = redis
    return r, redis, stream


@pytest.mark.asyncio
async def test_send_task_streams_when_first_event_arrives():
    first = {"type": "node.enter", "node": "plan_node", "thinkingBudget": 3000}
    result = {"ok": True, "state": {"final_answer": "42"}}
    r, redis, stream = _repl_with_mocks(first, [], result)

    await r._send_task("what is the answer?")

    redis.push_task.assert_awaited_once()
    stream.subscribe.assert_awaited_once()
    r._renderer.stream_live.assert_awaited_once_with(stream)
    stream.aclose.assert_awaited_once()
    # canonical answer still printed from get_result
    r._renderer.print_answer.assert_called_once()
    assert r._renderer.print_answer.call_args.args[0] == "42"


@pytest.mark.asyncio
async def test_send_task_falls_back_when_no_event():
    result = {"ok": True, "state": {"final_answer": "fallback-answer"}}
    r, redis, stream = _repl_with_mocks(None, [], result)  # first() -> None

    await r._send_task("hi")

    # no live streaming when no event arrived
    r._renderer.stream_live.assert_not_called()
    stream.aclose.assert_awaited_once()
    redis.get_result.assert_awaited()
    r._renderer.print_answer.assert_called_once()
    assert r._renderer.print_answer.call_args.args[0] == "fallback-answer"


@pytest.mark.asyncio
async def test_send_task_reports_error_result():
    result = {"ok": False, "error": "task_failed"}
    r, redis, stream = _repl_with_mocks(None, [], result)

    await r._send_task("boom")

    r._renderer.print_error.assert_called_once_with("task_failed")


@pytest.mark.asyncio
async def test_first_event_timeout_constant_is_small():
    assert 0 < FIRST_EVENT_TIMEOUT <= 5.0
```

- [ ] **Step 2: Run it (must fail)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/cli/test_repl_streaming.py -q
```

Expected: `ImportError: cannot import name 'FIRST_EVENT_TIMEOUT'`.

- [ ] **Step 3: Implement** — edit `services/cli/repl.py`

Add the constant near the top (after `HISTORY_PATH`):
```python
FIRST_EVENT_TIMEOUT = 2.0  # seconds to wait for the first stream event before falling back
```

Replace `_send_task()` with:
```python
    async def _send_task(self, task: str) -> None:
        task_id = str(uuid.uuid4())
        turn_session_id = str(uuid.uuid4())  # fresh per-turn session (Fix 4)
        self._sessions.append(SessionRecord(
            session_id=turn_session_id,
            workspace_id=self._ctx.workspace_id,
            workspace_name=self._ctx.workspace_name,
            task_preview=task[:120],
        ))

        try:  # Fix 6: error handling
            await self._redis.push_task(
                task_id=task_id,
                task=task,
                session_id=turn_session_id,
                user_id=self._ctx.identity.user_id,
                workspace_id=self._ctx.workspace_id,
            )
            stream = self._redis.subscribe_events(task_id)
            await stream.subscribe()
            try:
                first = await stream.first(timeout=FIRST_EVENT_TIMEOUT)
                if first is not None:
                    # Live path: render events as they arrive.
                    await self._renderer.stream_live(stream)
                    result = await self._redis.get_result(task_id, timeout=300.0)
                else:
                    # Fallback path: no events — behave exactly as before.
                    with self._renderer.thinking("Working…"):
                        result = await self._redis.get_result(task_id, timeout=300.0)
            finally:
                await stream.aclose()
        except Exception as exc:
            self._renderer.print_error(f"Connection error: {exc}")
            return

        if not result.get("ok"):
            self._renderer.print_error(result.get("error", "Unknown error"))
            return

        answer = extract_answer(result.get("state", {}))
        self._renderer.print_answer(answer, session_id=turn_session_id)
```

(`extract_answer` is already imported in repl.py.)

- [ ] **Step 4: Run tests (must pass)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/cli/test_repl_streaming.py -q
```

Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add services/cli/repl.py tests/services/cli/test_repl_streaming.py
git commit -m "feat(cli): REPL._send_task races live stream vs get_result fallback"
```

---

### Task 7: One-shot path in main.py mirrors the race

**Files:**
- Modify: `services/cli/main.py`
- Modify: `tests/services/cli/test_integration_smoke.py`

The one-shot (`python -m services.cli "task"`) path must use the same live/
fallback logic so a single command also streams. Factor the race into a shared
helper `run_task_with_streaming(client, renderer, task_id, ...)` in
`services/cli/event_stream.py` so both REPL and one-shot call one
implementation. Then refactor REPL (Task 6) and one-shot to use it.

- [ ] **Step 1: Write failing test** — append to `tests/services/cli/test_integration_smoke.py`

```python
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_run_task_with_streaming_live_path_returns_result():
    from services.cli.event_stream import run_task_with_streaming

    renderer = MagicMock()
    renderer.stream_live = AsyncMock()
    renderer.thinking = MagicMock()

    client = MagicMock()
    client.get_result = AsyncMock(return_value={"ok": True, "state": {"final_answer": "Z"}})

    stream = AsyncMock()
    stream.subscribe = AsyncMock()
    stream.first = AsyncMock(return_value={"type": "node.enter", "node": "plan_node"})
    stream.aclose = AsyncMock()
    client.subscribe_events = MagicMock(return_value=stream)

    result = await run_task_with_streaming(client, renderer, "t-1")

    renderer.stream_live.assert_awaited_once_with(stream)
    stream.aclose.assert_awaited_once()
    assert result == {"ok": True, "state": {"final_answer": "Z"}}


@pytest.mark.asyncio
async def test_run_task_with_streaming_fallback_path():
    from services.cli.event_stream import run_task_with_streaming

    renderer = MagicMock()
    renderer.stream_live = AsyncMock()

    # context-manager stub for thinking()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=None)
    cm.__exit__ = MagicMock(return_value=False)
    renderer.thinking = MagicMock(return_value=cm)

    client = MagicMock()
    client.get_result = AsyncMock(return_value={"ok": True, "state": {"final_answer": "fb"}})

    stream = AsyncMock()
    stream.subscribe = AsyncMock()
    stream.first = AsyncMock(return_value=None)  # timeout -> fallback
    stream.aclose = AsyncMock()
    client.subscribe_events = MagicMock(return_value=stream)

    result = await run_task_with_streaming(client, renderer, "t-2")

    renderer.stream_live.assert_not_called()
    renderer.thinking.assert_called_once_with("Working…")
    assert result["state"]["final_answer"] == "fb"
```

- [ ] **Step 2: Run it (must fail)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/cli/test_integration_smoke.py -q
```

Expected: `ImportError: cannot import name 'run_task_with_streaming'`.

- [ ] **Step 3: Implement** — append to `services/cli/event_stream.py`

```python
FIRST_EVENT_TIMEOUT = 2.0  # seconds to wait for the first event before fallback


async def run_task_with_streaming(client, renderer, task_id: str,
                                  result_timeout: float = 300.0) -> dict:
    """Shared live-vs-fallback driver for REPL and one-shot.

    Subscribes to the task's event channel; if the first event arrives within
    FIRST_EVENT_TIMEOUT, renders live and then reads the canonical result.
    Otherwise renders the spinner and reads the result (old behavior). Always
    returns the get_result() dict. Caller owns push_task() and printing.
    """
    stream = client.subscribe_events(task_id)
    await stream.subscribe()
    try:
        first = await stream.first(timeout=FIRST_EVENT_TIMEOUT)
        if first is not None:
            await renderer.stream_live(stream)
            return await client.get_result(task_id, timeout=result_timeout)
        with renderer.thinking("Working…"):
            return await client.get_result(task_id, timeout=result_timeout)
    finally:
        await stream.aclose()
```

Then refactor `REPL._send_task()` (Task 6) to call this helper instead of
inlining the race:
```python
            await self._redis.push_task(
                task_id=task_id, task=task, session_id=turn_session_id,
                user_id=self._ctx.identity.user_id,
                workspace_id=self._ctx.workspace_id,
            )
            from .event_stream import run_task_with_streaming
            result = await run_task_with_streaming(self._redis, self._renderer, task_id)
```

And refactor the one-shot block in `services/cli/main.py`:
```python
    if one_shot:
        from .event_stream import run_task_with_streaming
        client = LabmateRedisClient(redis_url)
        task_id = str(uuid.uuid4())
        _renderer.print_workspace(ws_choice_raw["name"], ws_choice_raw["workspace_id"])
        try:
            await client.push_task(
                task_id=task_id, task=one_shot, session_id=session_id,
                user_id=identity.user_id, workspace_id=ws_choice_raw["workspace_id"],
            )
            result = await run_task_with_streaming(client, _renderer, task_id)
        except Exception as exc:
            _renderer.print_error(f"Connection error: {exc}")
            await client.aclose()
            raise SystemExit(1)
        await client.aclose()
        if not result.get("ok"):
            _renderer.print_error(result.get("error", "unknown"))
            raise SystemExit(1)
        _renderer.print_answer(extract_answer(result.get("state", {})), session_id=session_id)
        return
```

> `FIRST_EVENT_TIMEOUT` now lives in `event_stream.py`. Update `repl.py`'s
> import in Task 6 to `from .event_stream import FIRST_EVENT_TIMEOUT,
> run_task_with_streaming` and remove the local constant defined in Task 6
> Step 3 (it was a stepping stone; the shared helper supersedes it). The
> `test_repl_streaming.py` import `from services.cli.repl import ...
> FIRST_EVENT_TIMEOUT` must be changed to re-export it: add
> `from .event_stream import FIRST_EVENT_TIMEOUT` to repl.py so the name is
> importable from both modules.

- [ ] **Step 4: Run tests (must pass)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/cli/ -q
```

Expected: all CLI tests pass (REPL streaming tests still green via the shared
helper; one-shot smoke green).

- [ ] **Step 5: Commit**

```bash
git add services/cli/event_stream.py services/cli/repl.py services/cli/main.py tests/services/cli/test_integration_smoke.py
git commit -m "feat(cli): shared streaming driver for REPL and one-shot paths"
```

---

### Task 8: Full CLI suite regression + fallback proof

**Files:**
- (no new files)

- [ ] **Step 1: Run the full CLI suite**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/cli/ -q
```

Expected: all pass.

- [ ] **Step 2: Prove the no-orchestrator fallback (independence from Plan 1)**

The `test_send_task_falls_back_when_no_event` and
`test_run_task_with_streaming_fallback_path` tests already simulate an
orchestrator that publishes no events (`stream.first` → `None`). Confirm they
pass in isolation:

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/cli/test_repl_streaming.py::test_send_task_falls_back_when_no_event tests/services/cli/test_integration_smoke.py::test_run_task_with_streaming_fallback_path -q
```

Expected: `2 passed` — proving the CLI works against an unmodified (Plan-1-less)
orchestrator.

- [ ] **Step 3: Commit (if any incidental fixes were needed)**

```bash
git add -A
git commit -m "test(cli): green streaming + fallback regression suite"
```

---

## Gaps / decisions made (surface before implementing)

1. **First-event timeout (decision).** `FIRST_EVENT_TIMEOUT = 2.0s` chooses
   live vs fallback. Too low and a slow orchestrator start looks like "no
   stream" and degrades to the spinner; too high and a truly Plan-1-less
   orchestrator stalls 2s before the spinner. **Confirm** 2s is acceptable, or
   make it env-configurable (`LABMATE_STREAM_WAIT`).

2. **Canonical answer still comes from `get_result()`.** Even on the live path
   we print the final answer from `get_result()` (not from accumulated
   `answer.delta`), because `extract_answer()` already handles the
   `final_answer` / `goal_tree.root.result` fallbacks and the result write is
   the source of truth. The streamed `answer_text` is display-only. **Confirm**
   this is desired (alternative: print `StreamRenderer.answer_text` directly and
   skip the second `get_result()` — but that loses the goal-tree fallback).

3. **`tool.*` events likely absent in v1.** Per Plan 1, the orchestrator won't
   emit `tool.start`/`tool.done` until sandbox calls are wrapped as LangChain
   tools. The renderer handles them correctly when present; the tool-row tests
   prove the rendering, but live runs may show none. Not blocking.

4. **Reasoning is shown inline, not collapsible.** The prompt asks for a
   "collapsible block." A terminal `Live` view cannot offer interactive
   collapse mid-stream; we render reasoning as dim-italic text and rely on it
   being visually distinct. **Confirm** inline dim-italic is acceptable, or
   defer true collapse to a post-turn redraw (out of scope here).

5. **Live transient=False.** The streamed frame stays on screen after the turn,
   then `print_answer()` prints the clean markdown answer below it. This double
   shows the answer (once streamed, once final). **Decision:** acceptable and
   Claude-Code-like (the streamed text is the "live" version, the final is the
   rendered markdown). **Confirm**, or set the answer block to clear before the
   final print.

6. **No `cancel` / Ctrl-C mid-stream handling.** The spec has a `cancel`
   ClientMsg, but the current CLI has no in-flight cancel. Ctrl-C during
   `stream_live` will raise `KeyboardInterrupt` out of `_send_task`'s try and be
   caught by the REPL loop's existing handler. **Confirm** no dedicated cancel
   is needed for v1.
