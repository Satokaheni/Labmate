# CLI Streaming Renderer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Labmate CLI subscribe to `labmate:events:<task_id>` (via XREAD BLOCK) and render live events — tool rows, reasoning blocks, streaming answer — while falling back to the existing spinner+`get_result()` path when no events arrive.

**Architecture:** `event_stream.py` already exists with `tail_events()` (XREAD BLOCK consumer). This plan adds: an `EventStream` class wrapping `tail_events()` with first-event detection; a `StreamRenderer` that reduces events into a Rich `Live` frame; a `stream_live()` method on `Renderer`; and a race in `REPL._send_task()` / `main.py` that goes live if the first event arrives within 2 s, otherwise falls back to today's spinner path. The orchestrator is unchanged — if it emits no events, the CLI silently falls back.

**Tech Stack:** Python, `rich` (`Live`, `Group`, `Text`, `Markdown`), `redis.asyncio` (XREAD BLOCK already in `tail_events()`), `asyncio.wait_for` (timeout on first event).

---

## Actual event contract (must match exactly)

All events share the envelope `{ "type": str, "task_id": str, "seq": int, "ts": float }` plus type-specific fields. The CLI handles these variants (others silently dropped):

```python
# turn.start — task accepted by orchestrator
{ "type": "turn.start", "task_id": str, "seq": int, "ts": float, "task": str }

# reasoning — one reasoning block per node (NOT streaming deltas)
{ "type": "reasoning", "task_id": str, "seq": int, "ts": float,
  "node": str, "summary": str, "text": str }   # summary ≤ 120 chars

# tool.start — before a skill or tool call (flat fields, no nested object)
{ "type": "tool.start", "task_id": str, "seq": int, "ts": float,
  "tool_id": str, "name": str, "kind": "skill" | "tool",
  "args": dict, "reasoning_why": str }

# tool.done — after a skill or tool call
{ "type": "tool.done", "task_id": str, "seq": int, "ts": float,
  "tool_id": str, "status": "done" | "error",
  "summary": str, "result": any, "duration_ms": int }

# answer.delta — one streaming chunk of the final answer
{ "type": "answer.delta", "task_id": str, "seq": int, "ts": float, "text": str }

# answer.done — full accumulated final answer (overrides any partial deltas)
{ "type": "answer.done", "task_id": str, "seq": int, "ts": float, "text": str }

# turn.done — task finished; signals EventStream.events() to stop
{ "type": "turn.done", "task_id": str, "seq": int, "ts": float,
  "status": "complete" | "error", "final_answer": str }
```

```python
# permission_request — orchestrator is paused waiting for user approval of a risky action
# The gate node (security-gate plan) emits this and blocks on labmate:permission:<session_id>.
# The CLI must render the action, collect y/s/n, and write the response back to that stream.
#
# risk_tier values:
#   "shell-exec"  — skill explicitly classified as shell-exec in SKILL_RISK_TABLE
#   "file-write"  — skill explicitly classified as file-write
#   "escalate"    — command_gate.analyze_command returned ESCALATE (unknown program,
#                   chaining, redirection, git write subcommand, etc.)
# "reason" is the human-readable explanation from analyze_command (d.reason), e.g.
#   "mkdir: requires approval" or "git: non-read-only subcommand 'push'"
# --auto-approve bypasses "escalate" and skill-tier prompts only.
# BLOCK decisions from command_gate are never surfaced here — they are denied inline.
{ "type": "permission_request", "task_id": str, "seq": int, "ts": float,
  "session_id": str,
  "risk_tier": "shell-exec" | "file-write" | "escalate",
  "skill": str, "tool": str,
  "preview": str,   # command string for shell-exec/escalate; "path\n<first 40 lines>" for file-write
  "reason": str }   # from analyze_command d.reason; empty string for skill-level risk_tier

# safety_warning — assess_safety node flagged input as suspicious; awaiting user confirm
{ "type": "safety_warning", "task_id": str, "seq": int, "ts": float,
  "session_id": str,
  "reason": str }    # one-sentence explanation from the LLM classifier

# safety_block — assess_safety node classified input as malicious; hard stop, no prompt
{ "type": "safety_block", "task_id": str, "seq": int, "ts": float,
  "reason": str }    # why it was blocked
```

**NOT emitted by the current orchestrator** (ignore in renderer):
- `node.enter`, `reasoning.delta` — superseded by `turn.start`/`reasoning`
- `context.update`, `agent.status` — not implemented

**Security gate dependency:** `permission_request`, `safety_warning`, and `safety_block` are
emitted by the security gate (see `docs/superpowers/plans/2026-06-21-security-gate.md`). The
gate does NOT need to be implemented before the CLI — if events of these types never arrive,
the renderer silently ignores them. Implement Task 9 of this plan alongside or after the
security gate plan.

---

## File structure

| File | Action | Responsibility |
|------|--------|---------------|
| `services/cli/event_stream.py` | Modify | Add `EVENTS_PREFIX`, `event_channel()`, `EventStream` class, `run_task_with_streaming()` |
| `services/cli/stream_renderer.py` | Create | Reduce events into a Rich `Live` frame |
| `services/cli/redis_client.py` | Modify | Store `_redis_url`; add `subscribe_events()` |
| `services/cli/renderer.py` | Modify | Add `stream_live()` |
| `services/cli/repl.py` | Modify | `_send_task()` races live stream vs fallback |
| `services/cli/main.py` | Modify | One-shot path uses same race |
| `tests/services/cli/test_event_stream.py` | Modify | Add `EventStream` + `run_task_with_streaming` tests |
| `tests/services/cli/test_stream_renderer.py` | Create | `StreamRenderer` reduction + render tests |
| `tests/services/cli/test_repl_streaming.py` | Create | `_send_task()` race + fallback tests |
| `tests/services/cli/test_integration_smoke.py` | Modify | `run_task_with_streaming` helper tests |
| `tests/services/cli/test_permission_prompt.py` | Create | y/s/n prompt, `send_permission_response`, `--auto-approve` flag (Task 9) |

---

### Task 1: Event channel helper in event_stream.py

**Files:**
- Modify: `services/cli/event_stream.py`
- Modify: `tests/services/cli/test_event_stream.py`

`event_stream.py` already exists with `tail_events()`. Add `EVENTS_PREFIX` and
`event_channel()` so the CLI never hard-codes the prefix string. The existing
`tail_events` test still passes unchanged.

- [ ] **Step 1: Write failing test** — append to `tests/services/cli/test_event_stream.py`

```python
def test_event_channel_constant_and_helper():
    from services.cli.event_stream import EVENTS_PREFIX, event_channel
    assert EVENTS_PREFIX == "labmate:events:"
    assert event_channel("t-1") == "labmate:events:t-1"
```

- [ ] **Step 2: Run it (must fail)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/cli/test_event_stream.py::test_event_channel_constant_and_helper -q
```

Expected: `ImportError: cannot import name 'EVENTS_PREFIX'`.

- [ ] **Step 3: Implement** — add to top of `services/cli/event_stream.py` (after existing imports)

```python
EVENTS_PREFIX = "labmate:events:"


def event_channel(task_id: str) -> str:
    return f"{EVENTS_PREFIX}{task_id}"
```

- [ ] **Step 4: Run all event_stream tests (must pass)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/cli/test_event_stream.py -q
```

Expected: all pass (existing `test_tail_events_decodes_and_advances` still green).

- [ ] **Step 5: Commit**

```bash
git add services/cli/event_stream.py tests/services/cli/test_event_stream.py
git commit -m "feat(cli): EVENTS_PREFIX and event_channel() helper in event_stream"
```

---

### Task 2: EventStream class — XREAD-based first-event detection

**Files:**
- Modify: `services/cli/event_stream.py`
- Modify: `tests/services/cli/test_event_stream.py`

`EventStream` wraps `tail_events()` with a first-event timeout (for live vs
fallback decision) and a buffered-replay mechanism so no event is lost.
Transport is XREAD BLOCK (not pub/sub). Tests mock `tail_events` directly.

- [ ] **Step 1: Write failing tests** — append to `tests/services/cli/test_event_stream.py`

```python
import asyncio
from unittest.mock import patch


async def _fake_gen(events):
    for e in events:
        yield e


@pytest.mark.asyncio
async def test_first_returns_parsed_event():
    evs = [{"type": "turn.start", "task": "hi", "seq": 1}]
    with patch("services.cli.event_stream.tail_events", return_value=_fake_gen(evs)):
        from services.cli.event_stream import EventStream
        stream = EventStream("redis://localhost:6379/0", "t-1")
        ev = await stream.first(timeout=1.0)
    assert ev is not None
    assert ev["type"] == "turn.start"


@pytest.mark.asyncio
async def test_first_returns_none_when_generator_empty():
    with patch("services.cli.event_stream.tail_events", return_value=_fake_gen([])):
        from services.cli.event_stream import EventStream
        stream = EventStream("redis://localhost:6379/0", "t-1")
        ev = await stream.first(timeout=0.2)
    assert ev is None


@pytest.mark.asyncio
async def test_first_returns_none_on_timeout():
    async def _blocking_gen():
        await asyncio.Event().wait()  # blocks forever
        yield {}  # never reached

    with patch("services.cli.event_stream.tail_events", return_value=_blocking_gen()):
        from services.cli.event_stream import EventStream
        stream = EventStream("redis://localhost:6379/0", "t-1")
        ev = await stream.first(timeout=0.05)
        await stream.aclose()
    assert ev is None


@pytest.mark.asyncio
async def test_events_replays_first_then_continues_until_turn_done():
    evs = [
        {"type": "turn.start", "task": "hi", "seq": 0},
        {"type": "answer.delta", "text": "hi", "seq": 1},
        {"type": "turn.done", "status": "complete", "seq": 2},
        {"type": "answer.delta", "text": "AFTER", "seq": 3},  # must NOT appear
    ]
    with patch("services.cli.event_stream.tail_events", return_value=_fake_gen(evs)):
        from services.cli.event_stream import EventStream
        stream = EventStream("redis://localhost:6379/0", "t-1")
        first = await stream.first(timeout=1.0)
        assert first["type"] == "turn.start"
        seen = [ev async for ev in stream.events()]
    types = [e["type"] for e in seen]
    assert types == ["turn.start", "answer.delta", "turn.done"]


@pytest.mark.asyncio
async def test_aclose_closes_generator():
    closed = []

    async def _closeable_gen():
        try:
            yield {"type": "turn.start", "seq": 0}
            await asyncio.sleep(100)
        except GeneratorExit:
            closed.append(True)

    with patch("services.cli.event_stream.tail_events", return_value=_closeable_gen()):
        from services.cli.event_stream import EventStream
        stream = EventStream("redis://localhost:6379/0", "t-1")
        await stream.first(timeout=1.0)
        await stream.aclose()
    assert closed == [True]
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
from typing import AsyncIterator, Optional


class EventStream:
    """Wraps tail_events() with first-event detection and turn.done termination.

    Usage:
        stream = EventStream(redis_url, task_id)
        first = await stream.first(timeout=2.0)   # None -> fall back to spinner
        if first is not None:
            async for ev in stream.events():      # replays first, then continues
                ...
        await stream.aclose()
    """

    def __init__(self, redis_url: str, task_id: str) -> None:
        self._redis_url = redis_url
        self._task_id = task_id
        self._gen = tail_events(redis_url, task_id)
        self._buffered: Optional[dict] = None

    async def first(self, timeout: float) -> Optional[dict]:
        """Return the first event within timeout seconds, or None.

        The returned event is buffered and replayed as the first item of
        events(), so it is never lost.
        """
        async def _next_event() -> Optional[dict]:
            try:
                return await self._gen.__anext__()
            except StopAsyncIteration:
                return None

        try:
            ev = await asyncio.wait_for(_next_event(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        if ev is not None:
            self._buffered = ev
        return ev

    async def events(self) -> AsyncIterator[dict]:
        """Yield events until (and including) turn.done, then stop."""
        if self._buffered is not None:
            ev, self._buffered = self._buffered, None
            yield ev
            if ev.get("type") == "turn.done":
                return
        async for ev in self._gen:
            yield ev
            if ev.get("type") == "turn.done":
                return

    async def aclose(self) -> None:
        await self._gen.aclose()
```

- [ ] **Step 4: Run tests (must pass)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/cli/test_event_stream.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add services/cli/event_stream.py tests/services/cli/test_event_stream.py
git commit -m "feat(cli): EventStream wraps tail_events with first-event detection"
```

---

### Task 3: StreamRenderer — reduce actual events into a Rich renderable

**Files:**
- Create: `services/cli/stream_renderer.py`
- Create: `tests/services/cli/test_stream_renderer.py`

Pure reducer: `handle(event)` mutates state, `render()` returns a `Group`.
Uses the **actual** event shapes from `services/orchestrator/events.py` —
flat `tool.start` fields (`tool_id`, `name`, `kind`, `args`, `reasoning_why`),
`tool.done` fields (`tool_id`, `duration_ms`), `reasoning` (not `reasoning.delta`),
`turn.start` instead of `node.enter`. No `context.update` or `agent.status`.

Glyphs:
- Active: `◆ working…` (shown while events flowing, hidden after `turn.done`)
- Tool running: `⚙ {name}  {reasoning_why[:60]}`
- Tool done: `✓ {name}  {summary}  ({sec}s)` or `✗` on error
- Reasoning: dim italic `{text}`
- Answer: streamed markdown text

- [ ] **Step 1: Write failing test** — `tests/services/cli/test_stream_renderer.py`

```python
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
```

- [ ] **Step 2: Run it (must fail)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/cli/test_stream_renderer.py -q
```

Expected: `ModuleNotFoundError: No module named 'services.cli.stream_renderer'`.

- [ ] **Step 3: Implement** — `services/cli/stream_renderer.py`

```python
from __future__ import annotations

from dataclasses import dataclass

from rich.console import Group
from rich.markdown import Markdown
from rich.text import Text


@dataclass
class _ToolRow:
    name: str
    label: str          # reasoning_why[:60] while running; summary once done
    status: str = "running"
    duration_ms: int = 0


class StreamRenderer:
    """Reduce orchestrator events into a renderable Rich frame."""

    def __init__(self) -> None:
        self._active: bool = False       # True after turn.start, False after turn.done
        self.reasoning_text: str = ""
        self.answer_text: str = ""
        self.done: bool = False
        self.status: str = ""
        self._tools: dict[str, _ToolRow] = {}
        self._tool_order: list[str] = []

    def handle(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "turn.start":
            self._active = True
        elif etype == "reasoning":
            text = event.get("text", "")
            if self.reasoning_text:
                self.reasoning_text += "\n" + text
            else:
                self.reasoning_text = text
        elif etype == "tool.start":
            tid = event.get("tool_id", "")
            self._tools[tid] = _ToolRow(
                name=event.get("name", "tool"),
                label=(event.get("reasoning_why") or "")[:60],
            )
            self._tool_order.append(tid)
        elif etype == "tool.done":
            tid = event.get("tool_id", "")
            row = self._tools.get(tid)
            if row is not None:
                row.status = event.get("status", "done")
                row.label = event.get("summary", row.label)
                row.duration_ms = event.get("duration_ms", 0)
        elif etype == "answer.delta":
            self.answer_text += event.get("text", "")
        elif etype == "answer.done":
            self.answer_text = event.get("text", self.answer_text)
        elif etype == "turn.done":
            self._active = False
            self.done = True
            self.status = event.get("status", "complete")
        # All other types: silently ignored

    def _tool_line(self, row: _ToolRow) -> Text:
        if row.status == "running":
            return Text(f"⚙ {row.name}  {row.label}", style="yellow")
        secs = f"{row.duration_ms / 1000:.1f}s"
        if row.status == "error":
            return Text(f"✗ {row.name}  {row.label}  ({secs})", style="red")
        return Text(f"✓ {row.name}  {row.label}  ({secs})", style="green")

    def render(self):
        parts: list = []

        if self._active:
            parts.append(Text("◆ working…", style="cyan"))

        for tid in self._tool_order:
            parts.append(self._tool_line(self._tools[tid]))

        if self.reasoning_text:
            parts.append(Text(self.reasoning_text, style="dim italic"))

        if self.answer_text:
            parts.append(Markdown(self.answer_text))

        if not parts:
            parts.append(Text("waiting…", style="dim"))

        return Group(*parts)
```

- [ ] **Step 4: Run tests (must pass)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/cli/test_stream_renderer.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add services/cli/stream_renderer.py tests/services/cli/test_stream_renderer.py
git commit -m "feat(cli): StreamRenderer reduces agent events into Rich frame"
```

---

### Task 4: LabmateRedisClient.subscribe_events()

**Files:**
- Modify: `services/cli/redis_client.py`
- Modify: `tests/services/cli/test_redis_client.py`

Store `redis_url` in `LabmateRedisClient.__init__` as `self._redis_url` so
`subscribe_events()` can pass it to `EventStream`. `get_result()` is untouched.

- [ ] **Step 1: Write failing test** — append to `tests/services/cli/test_redis_client.py`

```python
def test_subscribe_events_returns_event_stream():
    from services.cli.event_stream import EventStream
    client = LabmateRedisClient.__new__(LabmateRedisClient)
    client._redis_url = "redis://localhost:6379/0"

    stream = client.subscribe_events("t-99")
    assert isinstance(stream, EventStream)
    assert stream._task_id == "t-99"
    assert stream._redis_url == "redis://localhost:6379/0"
```

- [ ] **Step 2: Run it (must fail)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/cli/test_redis_client.py::test_subscribe_events_returns_event_stream -q
```

Expected: `AttributeError: 'LabmateRedisClient' object has no attribute 'subscribe_events'`.

- [ ] **Step 3: Implement** — edit `services/cli/redis_client.py`

In `__init__`, add `self._redis_url = url` (one line added):
```python
    def __init__(self, redis_url: str | None = None) -> None:
        url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self._redis_url = url
        self._redis = aioredis.from_url(url, decode_responses=False)
```

Add this method after `get_result`:
```python
    def subscribe_events(self, task_id: str) -> "EventStream":
        """Return an EventStream for labmate:events:<task_id> (XREAD BLOCK).

        If the orchestrator emits no events, the caller falls back to
        get_result() unchanged — this method never raises.
        """
        from .event_stream import EventStream
        return EventStream(self._redis_url, task_id)
```

- [ ] **Step 4: Run tests (must pass)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/cli/test_redis_client.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add services/cli/redis_client.py tests/services/cli/test_redis_client.py
git commit -m "feat(cli): LabmateRedisClient.subscribe_events() + store _redis_url"
```

---

### Task 5: Renderer.stream_live() — drive Live from an EventStream

**Files:**
- Modify: `services/cli/renderer.py`
- Modify: `tests/services/cli/test_renderer.py`

Add `stream_live()` to the existing `Renderer`. It drives the Rich `Live` loop,
feeds each event into a `StreamRenderer`, and returns the final `StreamRenderer`.
Does not subscribe or close the stream — the caller owns that lifecycle.

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
        {"type": "turn.start", "task": "compute"},
        {"type": "answer.delta", "text": "Hello "},
        {"type": "answer.delta", "text": "world"},
        {"type": "turn.done", "status": "complete", "final_answer": ""},
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

Add import at the top:
```python
from .stream_renderer import StreamRenderer
```

Add this method to `Renderer`:
```python
    async def stream_live(self, stream) -> StreamRenderer:
        """Drive a Rich Live frame from an EventStream.

        Returns the StreamRenderer so the caller can read accumulated
        answer/status. Does not subscribe or close `stream`.
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

### Task 6: shared run_task_with_streaming() helper

**Files:**
- Modify: `services/cli/event_stream.py`
- Create: `tests/services/cli/test_integration_smoke.py`

Factor the live-vs-fallback race into a shared helper in `event_stream.py`
so both REPL and one-shot call one implementation.

- [ ] **Step 1: Write failing test** — `tests/services/cli/test_integration_smoke.py`

```python
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_run_task_with_streaming_live_path_returns_result():
    from services.cli.event_stream import run_task_with_streaming

    renderer = MagicMock()
    renderer.stream_live = AsyncMock()

    client = MagicMock()
    client.get_result = AsyncMock(return_value={"ok": True, "state": {"final_answer": "Z"}})

    stream = MagicMock()
    stream.first = AsyncMock(return_value={"type": "turn.start", "task": "hi"})
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

    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=None)
    cm.__exit__ = MagicMock(return_value=False)
    renderer.thinking = MagicMock(return_value=cm)

    client = MagicMock()
    client.get_result = AsyncMock(return_value={"ok": True, "state": {"final_answer": "fb"}})

    stream = MagicMock()
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
FIRST_EVENT_TIMEOUT = 2.0  # seconds to wait before falling back to spinner


async def run_task_with_streaming(
    client, renderer, task_id: str, result_timeout: float = 300.0
) -> dict:
    """Race live stream vs fallback spinner; always return get_result() dict.

    Subscribes to the task's event channel. If the first event arrives within
    FIRST_EVENT_TIMEOUT, renders live then reads the canonical result. Otherwise
    renders the spinner and reads the result (original behavior). Caller owns
    push_task() and printing; this helper owns the stream lifecycle.
    """
    stream = client.subscribe_events(task_id)
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

- [ ] **Step 4: Run tests (must pass)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/cli/test_integration_smoke.py -q
```

Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add services/cli/event_stream.py tests/services/cli/test_integration_smoke.py
git commit -m "feat(cli): run_task_with_streaming shared live-vs-fallback helper"
```

---

### Task 7: REPL._send_task() and main.py one-shot path

**Files:**
- Modify: `services/cli/repl.py`
- Modify: `services/cli/main.py`
- Create: `tests/services/cli/test_repl_streaming.py`

Wire `run_task_with_streaming` into both entry points. `FIRST_EVENT_TIMEOUT`
lives in `event_stream.py` and is imported by the test directly from there.

- [ ] **Step 1: Write failing tests** — `tests/services/cli/test_repl_streaming.py`

```python
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from services.cli.repl import REPL, REPLContext
from services.cli.event_stream import FIRST_EVENT_TIMEOUT
from services.cli.identity import Identity


def _ctx():
    return REPLContext(
        identity=Identity(user_id="u-1", display_name="Tester"),
        workspace_id="ws-1", workspace_name="WS", workspace_paths=[],
        workspace_instructions=None, session_id="s-1",
        redis_url="redis://localhost:6379/0",
    )


def _repl_with_mocks(first_event, result):
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

    stream = MagicMock()
    stream.first = AsyncMock(return_value=first_event)
    stream.aclose = AsyncMock()
    redis.subscribe_events = MagicMock(return_value=stream)
    r._redis = redis
    return r, redis, stream


@pytest.mark.asyncio
async def test_send_task_streams_when_first_event_arrives():
    first = {"type": "turn.start", "task": "what is the answer?"}
    result = {"ok": True, "state": {"final_answer": "42"}}
    r, redis, stream = _repl_with_mocks(first, result)

    await r._send_task("what is the answer?")

    redis.push_task.assert_awaited_once()
    r._renderer.stream_live.assert_awaited_once_with(stream)
    stream.aclose.assert_awaited_once()
    r._renderer.print_answer.assert_called_once()
    assert r._renderer.print_answer.call_args.args[0] == "42"


@pytest.mark.asyncio
async def test_send_task_falls_back_when_no_event():
    result = {"ok": True, "state": {"final_answer": "fallback-answer"}}
    r, redis, stream = _repl_with_mocks(None, result)

    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=None)
    cm.__exit__ = MagicMock(return_value=False)
    r._renderer.thinking = MagicMock(return_value=cm)

    await r._send_task("hi")

    r._renderer.stream_live.assert_not_called()
    stream.aclose.assert_awaited_once()
    redis.get_result.assert_awaited()
    r._renderer.print_answer.assert_called_once()
    assert r._renderer.print_answer.call_args.args[0] == "fallback-answer"


@pytest.mark.asyncio
async def test_send_task_reports_error_result():
    result = {"ok": False, "error": "task_failed"}
    r, redis, stream = _repl_with_mocks(None, result)
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=None)
    cm.__exit__ = MagicMock(return_value=False)
    r._renderer.thinking = MagicMock(return_value=cm)

    await r._send_task("boom")

    r._renderer.print_error.assert_called_once_with("task_failed")


def test_first_event_timeout_constant_is_reasonable():
    assert 0 < FIRST_EVENT_TIMEOUT <= 5.0
```

- [ ] **Step 2: Run it (must fail)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/cli/test_repl_streaming.py -q
```

Expected: failures because `_send_task` doesn't call `subscribe_events` yet.

- [ ] **Step 3: Implement REPL** — edit `services/cli/repl.py`

Replace `_send_task` with:
```python
    async def _send_task(self, task: str) -> None:
        task_id = str(uuid.uuid4())
        turn_session_id = str(uuid.uuid4())
        self._sessions.append(SessionRecord(
            session_id=turn_session_id,
            workspace_id=self._ctx.workspace_id,
            workspace_name=self._ctx.workspace_name,
            task_preview=task[:120],
        ))

        try:
            await self._redis.push_task(
                task_id=task_id,
                task=task,
                session_id=turn_session_id,
                user_id=self._ctx.identity.user_id,
                workspace_id=self._ctx.workspace_id,
            )
            from .event_stream import run_task_with_streaming
            result = await run_task_with_streaming(self._redis, self._renderer, task_id)
        except Exception as exc:
            self._renderer.print_error(f"Connection error: {exc}")
            return

        if not result.get("ok"):
            self._renderer.print_error(result.get("error", "Unknown error"))
            return

        answer = extract_answer(result.get("state", {}))
        self._renderer.print_answer(answer, session_id=turn_session_id)
```

- [ ] **Step 4: Implement one-shot path** — edit `services/cli/main.py`

Replace the `if one_shot:` block:
```python
    if one_shot:
        from .event_stream import run_task_with_streaming
        client = LabmateRedisClient(redis_url)
        task_id = str(uuid.uuid4())
        _renderer.print_workspace(ws_choice_raw["name"], ws_choice_raw["workspace_id"])
        try:
            await client.push_task(
                task_id=task_id,
                task=one_shot,
                session_id=session_id,
                user_id=identity.user_id,
                workspace_id=ws_choice_raw["workspace_id"],
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

- [ ] **Step 5: Run tests (must pass)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/cli/test_repl_streaming.py -q
```

Expected: `4 passed`.

- [ ] **Step 6: Commit**

```bash
git add services/cli/repl.py services/cli/main.py tests/services/cli/test_repl_streaming.py
git commit -m "feat(cli): REPL and one-shot use run_task_with_streaming live race"
```

---

### Task 8: Full CLI suite regression + fallback proof

**Files:** none new

- [ ] **Step 1: Run the full CLI suite**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/cli/ -q
```

Expected: all pass.

- [ ] **Step 2: Prove the no-orchestrator fallback**

`test_send_task_falls_back_when_no_event` and
`test_run_task_with_streaming_fallback_path` simulate an orchestrator that
publishes no events (`stream.first` → `None`). Run in isolation:

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest \
  tests/services/cli/test_repl_streaming.py::test_send_task_falls_back_when_no_event \
  tests/services/cli/test_integration_smoke.py::test_run_task_with_streaming_fallback_path \
  -q
```

Expected: `2 passed`.

- [ ] **Step 3: Run the full project suite (regression)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest -q
```

Expected: all pass (orchestrator tests unchanged).

- [ ] **Step 4: Commit if any incidental fixes were needed**

```bash
git add -A
git commit -m "test(cli): full streaming + fallback regression suite"
```

---

### Task 9: Security gate event handlers — permission prompt + safety events

**Context:** This task implements the CLI side of the security gate
(`docs/superpowers/plans/2026-06-21-security-gate.md`). The gate node in the
orchestrator emits `permission_request`, `safety_warning`, and `safety_block`
events and blocks waiting for a response on `labmate:permission:<session_id>`.
The CLI must handle those events, prompt the user, and write the response back.

**Because the CLI is being built from scratch here, implement this now rather
than as a patch on top later. Tasks 8 and 9 of the security gate plan are
superseded by this task when the CLI is built fresh.**

**Files:**
- Modify: `services/cli/stream_renderer.py` (add handlers for three new event types)
- Modify: `services/cli/redis_client.py` (add `send_permission_response()`)
- Modify: `services/cli/repl.py` (thread `auto_approve` through session, add `awaiting_safety_confirm` state)
- Modify: `services/cli/main.py` (add `--auto-approve` CLI flag)
- Create: `tests/services/cli/test_permission_prompt.py`

- [ ] **Step 1: Write failing tests**

Create `tests/services/cli/test_permission_prompt.py`:

```python
from __future__ import annotations
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from services.cli.redis_client import LabmateRedisClient


@pytest.mark.asyncio
async def test_send_permission_response_granted():
    client = LabmateRedisClient.__new__(LabmateRedisClient)
    client._redis = AsyncMock()
    client._redis.xadd = AsyncMock()
    await client.send_permission_response("sess-1", "y")
    client._redis.xadd.assert_called_once()
    call_args = client._redis.xadd.call_args
    stream = call_args[0][0]
    payload = call_args[0][1]
    assert stream == "labmate:permission:sess-1"
    assert payload["choice"] == "y"


@pytest.mark.asyncio
async def test_send_permission_response_session_approve():
    client = LabmateRedisClient.__new__(LabmateRedisClient)
    client._redis = AsyncMock()
    client._redis.xadd = AsyncMock()
    await client.send_permission_response("sess-1", "s")
    payload = client._redis.xadd.call_args[0][1]
    assert payload["choice"] == "s"


@pytest.mark.asyncio
async def test_send_permission_response_denied():
    client = LabmateRedisClient.__new__(LabmateRedisClient)
    client._redis = AsyncMock()
    client._redis.xadd = AsyncMock()
    await client.send_permission_response("sess-1", "n")
    payload = client._redis.xadd.call_args[0][1]
    assert payload["choice"] == "n"


def test_stream_renderer_handles_permission_request(capsys):
    from services.cli.stream_renderer import StreamRenderer
    renderer = StreamRenderer()
    event = {
        "type": "permission_request",
        "task_id": "t1", "seq": 1, "ts": 0.0,
        "session_id": "sess-1",
        "risk_tier": "shell-exec",
        "skill": "code-sandbox", "tool": "run_bash",
        "preview": "pytest tests/ -v",
    }
    # Should return pending permission state, not raise
    result = renderer.handle_permission_request(event)
    assert result["pending"] is True
    assert result["session_id"] == "sess-1"


def test_stream_renderer_handles_safety_block(capsys):
    from services.cli.stream_renderer import StreamRenderer
    renderer = StreamRenderer()
    event = {
        "type": "safety_block",
        "task_id": "t1", "seq": 1, "ts": 0.0,
        "reason": "Detected destructive shell pattern: rm -rf /",
    }
    # Should mark the stream as terminated, not raise
    result = renderer.handle_safety_block(event)
    assert result["blocked"] is True


def test_stream_renderer_handles_safety_warning(capsys):
    from services.cli.stream_renderer import StreamRenderer
    renderer = StreamRenderer()
    event = {
        "type": "safety_warning",
        "task_id": "t1", "seq": 1, "ts": 0.0,
        "session_id": "sess-1",
        "reason": "Task may attempt credential access.",
    }
    result = renderer.handle_safety_warning(event)
    assert result["pending"] is True
    assert result["session_id"] == "sess-1"
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/services/cli/test_permission_prompt.py -v
```
Expected: FAIL — `send_permission_response` missing; `handle_permission_request` missing.

- [ ] **Step 3: Add `send_permission_response` to `redis_client.py`**

In `services/cli/redis_client.py`, add:

```python
async def send_permission_response(self, session_id: str, choice: str) -> None:
    """Write user's permission choice back to the gate's response stream.

    choice: "y" (approve once), "s" (approve session), "n" (deny)
    """
    await self._redis.xadd(
        f"labmate:permission:{session_id}",
        {"choice": choice},
    )
```

- [ ] **Step 4: Add permission/safety handlers to `stream_renderer.py`**

In `services/cli/stream_renderer.py`, add these three methods to `StreamRenderer`:

```python
def handle_permission_request(self, event: dict) -> dict:
    """Pause live view and surface a permission prompt. Returns pending state
    so the caller (stream_live) can collect y/s/n and write the response."""
    risk_tier = event.get("risk_tier", "")
    skill = event.get("skill", "")
    tool = event.get("tool", "")
    preview = event.get("preview", "")
    reason = event.get("reason", "")
    tier_labels = {
        "shell-exec": "[bold yellow]shell-exec[/]",
        "file-write": "[bold cyan]file-write[/]",
        "escalate": "[bold magenta]escalate[/]",
    }
    tier_label = tier_labels.get(risk_tier, f"[bold]{risk_tier}[/]")
    self.console.print(f"\n[bold]Permission required:[/] {tier_label}  {skill} › {tool}")
    if reason:
        self.console.print(f"[dim]{reason}[/]")
    self.console.print(f"[dim]{preview[:800]}[/]")
    self.console.print("\n  [bold][y][/] Yes")
    self.console.print("  [bold][s][/] Yes, allow this session")
    self.console.print("  [bold][n][/] No\n")
    return {"pending": True, "session_id": event.get("session_id", "")}

def handle_safety_block(self, event: dict) -> dict:
    """Render a hard-stop safety block message. No prompt — task is cancelled."""
    reason = event.get("reason", "")
    self.console.print(f"\n[bold red]⛔ Task blocked:[/] {reason}\n")
    return {"blocked": True}

def handle_safety_warning(self, event: dict) -> dict:
    """Surface a safety warning and return pending state for y/n confirmation."""
    reason = event.get("reason", "")
    self.console.print(f"\n[bold yellow]⚠ Safety warning:[/] {reason}")
    self.console.print("\n  [bold][y][/] Continue anyway")
    self.console.print("  [bold][n][/] Cancel\n")
    return {"pending": True, "session_id": event.get("session_id", "")}
```

In `StreamRenderer.handle()` (the main event dispatch method), add three cases:

```python
elif event_type == "permission_request":
    pending = self.handle_permission_request(event)
    # Signal caller to collect input — do not return yet
    return pending
elif event_type == "safety_warning":
    pending = self.handle_safety_warning(event)
    return pending
elif event_type == "safety_block":
    return self.handle_safety_block(event)
```

- [ ] **Step 5: Thread permission response through `stream_live()` in `renderer.py`**

In `services/cli/renderer.py`, update `stream_live()` to handle `pending` events returned
by `StreamRenderer.handle()`. When `pending=True`, pause the `Live` context, read a
single keypress (`y`/`s`/`n`), call `redis_client.send_permission_response(session_id, choice)`,
then resume:

```python
async def _collect_keypress(self, valid: str = "ysn") -> str:
    """Read a single character from stdin (no Enter needed)."""
    import sys, tty, termios
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while True:
            ch = sys.stdin.read(1).lower()
            if ch in valid:
                return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

# Inside stream_live(), after each `result = self._stream_renderer.handle(event)`:
if result and result.get("pending"):
    session_id = result["session_id"]
    choice = await self._collect_keypress()
    await self._redis_client.send_permission_response(session_id, choice)
    if choice == "s":
        self.console.print("[dim]Auto-approve enabled for this session.[/]")
        self._auto_approve = True
    elif choice == "n":
        self.console.print("[dim]Action skipped.[/]")
```

- [ ] **Step 6: Add `--auto-approve` flag to `main.py`**

In `services/cli/main.py`, add a `--auto-approve` / `-A` flag via typer and pass it into
the initial Redis push payload as `"auto_approve": True` so the orchestrator's gate node
skips prompting for the entire session.

**Scope of `--auto-approve`:** It bypasses the y/s/n prompt for `"shell-exec"`, `"file-write"`,
and `"escalate"` risk tiers. It does **not** bypass `BLOCK` decisions from `command_gate.analyze_command`
— those are always denied inline before reaching the gate node and never surface as
`permission_request` events. Auto-approve is for scripted/batch use; it does not disable
the structural command gate.

```python
@app.command()
def main(
    ...existing args...,
    auto_approve: bool = typer.Option(
        False, "--auto-approve", "-A",
        help="Skip all permission prompts for this session (scripted/batch use).",
    ),
):
    ...
    payload = {
        ...,
        "auto_approve": auto_approve,
    }
```

In `services/cli/repl.py`, thread `auto_approve` through `REPLContext` so the REPL
sets it in every task payload pushed to `labmate:goals`.

- [ ] **Step 7: Run tests**

```bash
python -m pytest tests/services/cli/test_permission_prompt.py -v
```
Expected: PASS (7 tests).

```bash
python -m pytest tests/services/cli/ -v
```
Expected: PASS — no regressions in existing streaming tests.

- [ ] **Step 8: Commit**

```bash
git add services/cli/stream_renderer.py services/cli/redis_client.py \
        services/cli/renderer.py services/cli/repl.py services/cli/main.py \
        tests/services/cli/test_permission_prompt.py
git commit -m "feat(security): CLI permission prompt + safety event handlers

- send_permission_response() on LabmateRedisClient
- StreamRenderer: handle_permission_request, handle_safety_warning, handle_safety_block
- stream_live(): pause Live, collect y/s/n keypress, write response to gate stream
- --auto-approve / -A flag seeds auto_approve=True in task payload

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Decisions made (surface before implementing)

1. **First-event timeout (2.0 s).** Too low and a slow orchestrator looks like
   "no stream" (degrades to spinner). Too high and a Plan-1-less orchestrator
   stalls 2 s. 2.0 s is acceptable; make it `LABMATE_STREAM_WAIT` env-configurable
   in a follow-up if needed.

2. **Canonical answer from `get_result()`.** Even on the live path, the final
   answer is printed from `get_result()` (not `StreamRenderer.answer_text`),
   because `extract_answer()` already handles the `final_answer`/`goal_tree`
   fallbacks. The streamed text is display-only.

3. **Reasoning shown inline (dim italic).** `reasoning` events carry a full
   `text` block, not streaming deltas. Multiple reasoning events append with a
   newline. No interactive collapse in a terminal Live view — inline dim text is
   acceptable.

4. **Live `transient=False`.** The streamed frame stays on screen, then
   `print_answer()` prints the clean markdown below it. Acceptable
   (Claude-Code-like). To suppress the streamed view, set `transient=True` —
   out of scope here.

5. **No Ctrl-C mid-stream handling.** `KeyboardInterrupt` during `stream_live`
   propagates out of `_send_task`'s try and is caught by the REPL loop's
   existing handler. No dedicated cancel needed for v1.
