# Agent Event Stream (tool selection · lifecycle · reasoning) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the orchestrator emit a live, transport-agnostic event stream so any client (CLI or future WebSocket frontend) can see (1) which skill/tool was selected for a task, (2) when each starts running and finishes, and (3) the model's reasoning for the call — for debugging.

**Architecture:** The orchestrator publishes JSON events to a per-task **Redis Stream** `labmate:events:<task_id>` (`XADD`). This is transport-neutral: a CLI tails it with `XREAD BLOCK`; a future WebSocket gateway tails it the same way and relays frames. A task-scoped `EventEmitter` is held in a `contextvars.ContextVar` set once per goal in `main._handle`, so the deeply-nested emit sites (skill router, ReAct executor) emit without threading an emitter through every signature. When no emitter is set (unit tests), emission is a no-op. Event shapes are a subset of `FRONTEND_SPEC.md §4 StreamEvent` so the frontend later consumes identical JSON.

**Tech Stack:** Python 3.12, `redis.asyncio` (pinned `redis>=5.0,<6`), `litellm` (Gemma 4 via llama.cpp, OpenAI-compatible), `pytest` + `pytest-asyncio`. No new dependencies.

## Global Constraints

- redis client: `import redis.asyncio as aioredis`; pin stays `redis>=5.0,<6` (CLAUDE.md §5; 8.x regressed blocking reads).
- Every `litellm.acompletion` call passes `api_key="not-needed"` and explicit `extra_body={"thinking_budget_tokens": N}` (CLAUDE.md §6).
- Reasoning comes from `response.choices[0].message.reasoning_content` (server runs `--reasoning-format deepseek`; verified live). `reasoning_content` and `content` are DISTINCT — never merge them (FRONTEND_SPEC.md §9.2).
- NEVER write to stdout in orchestrator/skill code — logging goes to stderr only (CLAUDE.md §1).
- Do NOT modify repo-root `core/`, `tools/`, or root `main.py` (M2 baseline). `services/orchestrator/*` and `services/cli/*` are fair game.
- Emission MUST be best-effort: a Redis/emit failure must never break task execution (wrap every emit in try/except inside the emitter).
- Event channel name: `labmate:events:<task_id>` (Redis Stream). Result blob (`labmate:result:<task_id>`) and goals stream are unchanged.
- Scope: ONLY the 3 asks (tool selection, run/finish lifecycle, reasoning). No auth, artifacts, context accounting, modes, or WebSocket gateway.

---

## File Structure

- **Create** `services/orchestrator/events.py` — event schema builders, `EventEmitter`, the `current_emitter` ContextVar, a module-level `emit(...)` helper (no-op when unset), and `extract_reasoning(response)`. One responsibility: "how an event is shaped and published."
- **Modify** `services/orchestrator/main.py` — in `_handle`, create an `EventEmitter(self._redis, task_id)`, set the ContextVar, emit `turn.start` before `run_task` and `turn.done` after (in `finally`), reset the ContextVar.
- **Modify** `services/orchestrator/skill_router.py` — capture `reasoning_content` in `select()`; emit a `reasoning` event there; in `run()` emit `tool.start` (with `reasoning_why`) before `execute` and `tool.done` after.
- **Modify** `services/orchestrator/coding_orchestrator.py` — in `react_execute`, emit `tool.start`/`tool.done` for each `run_bash` / `call_skill_tool` the ReAct loop dispatches, and a `reasoning` event per assistant turn that carries `reasoning_content`.
- **Create** `services/cli/event_stream.py` — a reusable async consumer (`tail_events(redis_url, task_id)`) that `XREAD BLOCK`s the stream and yields decoded events; doubles as the reference consumer for the CLI and the live test.
- **Create** `tests/services/orchestrator/test_events.py` — unit tests for builders, emitter, `extract_reasoning`, and the no-op contextvar path.
- **Modify** `tests/services/orchestrator/test_skill_router.py` — add tests asserting `select`/`run` emit the expected events when an emitter is set.

Event types emitted (transport-neutral JSON; all carry `type`, `task_id`, `seq`, `ts`):

| type | when | key fields |
|---|---|---|
| `turn.start` | goal pulled, before run_task | `task` |
| `reasoning` | after any reasoning-bearing LLM call | `node`, `summary`, `text` |
| `tool.start` | a skill/tool is selected & about to run | `tool_id`, `name`, `kind` (`skill`\|`tool`), `args`, `reasoning_why` |
| `tool.done` | that skill/tool returns | `tool_id`, `status` (`done`\|`error`), `summary`, `result`, `duration_ms` |
| `turn.done` | run_task returns/raises | `status` (`complete`\|`error`), `final_answer` |

---

## Task 1: Event schema, emitter, and reasoning helper

**Files:**
- Create: `services/orchestrator/events.py`
- Test: `tests/services/orchestrator/test_events.py`

**Interfaces:**
- Produces:
  - `current_emitter: ContextVar[EventEmitter | None]` (default `None`)
  - `class EventEmitter` with `__init__(self, redis, task_id: str)` and `async def emit(self, type: str, **fields) -> None`
  - `async def emit(type: str, **fields) -> None` — module-level; reads `current_emitter`, no-op if `None`
  - `def extract_reasoning(response) -> str` — returns `choices[0].message.reasoning_content` or `""`
  - `def reasoning_summary(text: str) -> str` — first non-empty line, trimmed to 120 chars
  - `EVENTS_STREAM_PREFIX = "labmate:events:"`
- Consumes: nothing.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/orchestrator/test_events.py
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
    # No emitter set → must not raise, must not require redis
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_events.py -q -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.orchestrator.events'`

- [ ] **Step 3: Write the implementation**

```python
# services/orchestrator/events.py
"""
Transport-agnostic agent event stream.

The orchestrator publishes JSON events to a per-task Redis Stream
`labmate:events:<task_id>`. Any client tails it with XREAD BLOCK (CLI directly;
a future WebSocket gateway relays). Event shapes are a subset of
FRONTEND_SPEC.md §4 StreamEvent.

A task-scoped EventEmitter lives in the `current_emitter` ContextVar, set once
per goal in main._handle, so deeply-nested emit sites (skill router, ReAct
executor) call the module-level `emit()` without threading an emitter through
every signature. No emitter set (e.g. unit tests) => emit is a no-op.

CRITICAL: emission is best-effort. A Redis failure must never break a task, so
every XADD is wrapped in try/except. Never write to stdout — log to stderr.
"""
from __future__ import annotations

import json
import logging
import time
from contextvars import ContextVar
from typing import Any

import redis.asyncio as aioredis

_log = logging.getLogger("events")

EVENTS_STREAM_PREFIX = "labmate:events:"
EVENTS_MAXLEN = 2000  # cap stream length (approx) so it can't grow unbounded

current_emitter: ContextVar["EventEmitter | None"] = ContextVar(
    "current_emitter", default=None
)


def extract_reasoning(response: Any) -> str:
    """Pull message.reasoning_content from a litellm response; '' if absent.

    The server runs with --reasoning-format deepseek, so reasoning is in
    message.reasoning_content (DISTINCT from message.content). Defensive: tolerate
    missing choices/message/attr.
    """
    try:
        choices = getattr(response, "choices", None)
        if not choices:
            return ""
        message = getattr(choices[0], "message", None)
        rc = getattr(message, "reasoning_content", None) if message else None
        return rc or ""
    except Exception:
        return ""


def reasoning_summary(text: str) -> str:
    """First non-empty line of reasoning, trimmed to 120 chars (for collapsed UI)."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:120]
    return (text or "")[:120]


class EventEmitter:
    """Publishes ordered task events to a per-task Redis Stream (best-effort)."""

    def __init__(self, redis: aioredis.Redis, task_id: str) -> None:
        self._redis = redis
        self._task_id = task_id
        self._seq = 0

    async def emit(self, type: str, **fields: Any) -> None:
        self._seq += 1
        evt = {
            "type": type,
            "task_id": self._task_id,
            "seq": self._seq,
            "ts": time.time(),
            **fields,
        }
        try:
            await self._redis.xadd(
                f"{EVENTS_STREAM_PREFIX}{self._task_id}",
                {"event": json.dumps(evt, default=str)},
                maxlen=EVENTS_MAXLEN,
                approximate=True,
            )
        except Exception as exc:  # never let telemetry break execution
            _log.warning("event emit failed (%s): %s", type, exc)


async def emit(type: str, **fields: Any) -> None:
    """Module-level emit: routes to the task-scoped emitter, or no-ops."""
    em = current_emitter.get()
    if em is None:
        return
    await em.emit(type, **fields)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_events.py -q -p no:cacheprovider`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/events.py tests/services/orchestrator/test_events.py
git commit -m "feat(events): transport-agnostic agent event stream (emitter + schema + reasoning helper)"
```

---

## Task 2: Emitter lifecycle in the goal handler

**Files:**
- Modify: `services/orchestrator/main.py` (inside `OrchestratorProcess._handle`)
- Test: `tests/services/orchestrator/test_main.py`

**Interfaces:**
- Consumes: `events.EventEmitter`, `events.current_emitter`, `events.emit` (Task 1).
- Produces: `turn.start` emitted before `orch.run_task`; `turn.done` emitted in `finally` with `status` and `final_answer`. The `current_emitter` ContextVar is set for the duration of the handler and reset after.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/orchestrator/test_main.py  (add this test)
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from services.orchestrator.main import OrchestratorProcess


@pytest.mark.asyncio
async def test_handle_emits_turn_start_and_done():
    proc = OrchestratorProcess()
    proc._redis = MagicMock()
    proc._redis.xadd = AsyncMock()
    proc._redis.set = AsyncMock()
    proc._redis.publish = AsyncMock()
    proc._redis.xack = AsyncMock()

    orch = MagicMock()
    orch.run_task = AsyncMock(return_value={"final_answer": "done", "error": None})
    storage = MagicMock()

    fields = {"payload": json.dumps({"task_id": "t-1", "task": "do it", "session_id": "t-1"})}
    await proc._handle("1-0", fields, orch, storage)

    streams = [c.args[0] for c in proc._redis.xadd.await_args_list]
    assert "labmate:events:t-1" in streams
    types = [json.loads(c.args[1]["event"])["type"] for c in proc._redis.xadd.await_args_list]
    assert types[0] == "turn.start"
    assert "turn.done" in types
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_main.py::test_handle_emits_turn_start_and_done -q -p no:cacheprovider`
Expected: FAIL (no `turn.start` emitted)

- [ ] **Step 3: Implement — wire emitter into `_handle`**

At the top of `services/orchestrator/main.py` add the import:

```python
from services.orchestrator import events
```

In `_handle`, immediately after `task_id`, `task_text`, `session_id` are parsed (just after the `payload = json.loads(...)` block that sets `task_id`), insert:

```python
            _emitter = events.EventEmitter(self._redis, task_id)
            _token = events.current_emitter.set(_emitter)
            await _emitter.emit("turn.start", task=task_text)
```

Wrap the existing `run_task` + result-write body so the `finally` emits `turn.done` and resets the ContextVar. Concretely, in the existing `finally:` block of `_handle` (where `xack` happens), add BEFORE the `xack` line:

```python
            try:
                _status = "complete" if task_succeeded and (
                    not isinstance(final_state, dict) or final_state.get("error") is None
                ) else "error"
                _answer = ""
                if isinstance(final_state, dict):
                    _answer = final_state.get("final_answer") or ""
                await events.emit("turn.done", status=_status, final_answer=_answer)
            except Exception:
                pass
            events.current_emitter.reset(_token)
```

(Define `_token`/`_emitter` before the `try` with safe defaults — `_token = None` — and guard `if _token is not None` around the reset, mirroring the file's existing "bind before try" pattern so `finally` never raises `NameError`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_main.py -q -p no:cacheprovider`
Expected: PASS (existing main tests + the new one)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/main.py tests/services/orchestrator/test_main.py
git commit -m "feat(events): emit turn.start/turn.done and scope the emitter per goal"
```

---

## Task 3: Skill router emits reasoning + tool.start/tool.done

**Files:**
- Modify: `services/orchestrator/skill_router.py`
- Test: `tests/services/orchestrator/test_skill_router.py`

**Interfaces:**
- Consumes: `events.emit`, `events.extract_reasoning`, `events.reasoning_summary` (Task 1).
- Produces: `select()` stores the last selection reasoning in `self._last_reasoning: str` and emits a `reasoning` event (node `"route"`). `run()` emits `tool.start` (`kind="skill"`, `reasoning_why=self._last_reasoning`, `args` from the plan) before `execute`, and `tool.done` after with `status`, `summary`, `result`, `duration_ms`. `select()`'s return type is unchanged (`str | None`).

- [ ] **Step 1: Write the failing test**

```python
# tests/services/orchestrator/test_skill_router.py  (add)
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from services.orchestrator import events
from services.orchestrator.skill_router import SkillRouter


@pytest.mark.asyncio
async def test_run_emits_tool_start_and_done(router):
    # router fixture already builds a SkillRouter with a mock runner/redis
    router.select = AsyncMock(return_value="pdf-parse")
    router._last_reasoning = "the task asks to parse a PDF"
    router.plan_tool_call = AsyncMock(return_value={"tool": "parse", "arguments": {"path": "x.pdf"}})
    router.execute = AsyncMock(return_value={"ok": True, "result": {"content": [{"text": "# md"}]}})

    captured = []

    class FakeEmitter:
        async def emit(self, type, **f):
            captured.append({"type": type, **f})

    token = events.current_emitter.set(FakeEmitter())
    try:
        await router.run("parse the pdf x.pdf")
    finally:
        events.current_emitter.reset(token)

    types = [e["type"] for e in captured]
    assert "tool.start" in types and "tool.done" in types
    start = next(e for e in captured if e["type"] == "tool.start")
    assert start["name"] == "pdf-parse"
    assert start["kind"] == "skill"
    assert start["reasoning_why"] == "the task asks to parse a PDF"
    done = next(e for e in captured if e["type"] == "tool.done")
    assert done["status"] == "done"
    assert "tool_id" in start and start["tool_id"] == done["tool_id"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_skill_router.py::test_run_emits_tool_start_and_done -q -p no:cacheprovider`
Expected: FAIL (no tool.start emitted)

- [ ] **Step 3: Implement**

Add imports at the top of `skill_router.py`:

```python
import time
import uuid
from services.orchestrator import events
```

In `__init__`, add: `self._last_reasoning: str = ""`.

In `select()`, where it parses a successful tool call (right before `return skill_name`), capture and emit reasoning:

```python
                    if skill_name and skill_name in self._runner.catalog:
                        self._last_reasoning = events.extract_reasoning(r)
                        await events.emit(
                            "reasoning",
                            node="route",
                            summary=events.reasoning_summary(self._last_reasoning),
                            text=self._last_reasoning,
                        )
                        _log.info("selected skill: %s (attempt %d)", skill_name, attempt + 1)
                        return skill_name
```

Rewrite `run()` to emit lifecycle around `execute`:

```python
    async def run(self, task: str) -> dict | None:
        skill_name = await self.select(task)
        if skill_name is None:
            return None
        plan = await self.plan_tool_call(task, skill_name)
        if plan is None:
            return None

        tool_id = uuid.uuid4().hex[:12]
        await events.emit(
            "tool.start",
            tool_id=tool_id,
            name=skill_name,
            kind="skill",
            args=plan.get("arguments", {}),
            reasoning_why=self._last_reasoning,
        )
        started = time.monotonic()
        try:
            result = await self.execute(skill_name, plan["tool"], plan["arguments"])
        except Exception as exc:
            await events.emit(
                "tool.done", tool_id=tool_id, status="error",
                summary=str(exc)[:200], result=None,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            raise
        ok = bool(result.get("ok"))
        await events.emit(
            "tool.done",
            tool_id=tool_id,
            status="done" if ok else "error",
            summary=("ok" if ok else str(result.get("error", "failed")))[:200],
            result=result.get("result"),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_skill_router.py -q -p no:cacheprovider`
Expected: PASS (existing + new)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/skill_router.py tests/services/orchestrator/test_skill_router.py
git commit -m "feat(events): skill router emits reasoning + tool.start/tool.done"
```

---

## Task 4: ReAct executor emits lifecycle + reasoning for its tool calls

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py` (inside `AsyncOrchestrator.react_execute`)
- Test: `tests/services/orchestrator/test_coding_orchestrator.py`

**Interfaces:**
- Consumes: `events.emit`, `events.extract_reasoning`, `events.reasoning_summary` (Task 1).
- Produces: within `react_execute`'s ReAct loop, for each `run_bash` / `call_skill_tool` tool call the model emits, emit `tool.start` (`kind="tool"` for run_bash, `kind="skill"` for call_skill_tool; `reasoning_why` = that assistant turn's `reasoning_content`) before dispatch and `tool.done` after. The skill-first `run()` path already emits via Task 3 (do not double-emit there).

- [ ] **Step 1: Write the failing test**

```python
# tests/services/orchestrator/test_coding_orchestrator.py  (add)
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from services.orchestrator import events
from services.orchestrator.coding_orchestrator import AsyncOrchestrator


def _msg_with_tool_call(name, arguments_json, reasoning=""):
    tc = MagicMock()
    tc.id = "call-1"
    tc.function = MagicMock(name=name)
    tc.function.name = name
    tc.function.arguments = arguments_json
    msg = MagicMock()
    msg.tool_calls = [tc]
    msg.content = ""
    msg.reasoning_content = reasoning
    msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    return msg


@pytest.mark.asyncio
async def test_react_execute_emits_tool_events_for_run_bash():
    orch = AsyncOrchestrator(skill_router=None, mcp=MagicMock())
    orch.mcp.call_tool = AsyncMock(return_value=MagicMock(content=[MagicMock(text="hi")], isError=False))

    # turn 1: model calls run_bash; turn 2: model finishes
    resp1 = MagicMock(choices=[MagicMock(message=_msg_with_tool_call("run_bash", '{"command":"echo hi"}', "need shell"))])
    finish_msg = MagicMock(tool_calls=None, content="done")
    finish_msg.model_dump = lambda: {"role": "assistant", "content": "done"}
    resp2 = MagicMock(choices=[MagicMock(message=finish_msg)])

    captured = []
    class FakeEmitter:
        async def emit(self, type, **f): captured.append({"type": type, **f})

    with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
               new_callable=AsyncMock, side_effect=[resp1, resp2]):
        token = events.current_emitter.set(FakeEmitter())
        try:
            await orch.react_execute("run echo")
        finally:
            events.current_emitter.reset(token)

    types = [e["type"] for e in captured]
    assert "tool.start" in types and "tool.done" in types
    start = next(e for e in captured if e["type"] == "tool.start")
    assert start["name"] == "run_bash" and start["kind"] == "tool"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_coding_orchestrator.py::test_react_execute_emits_tool_events_for_run_bash -q -p no:cacheprovider`
Expected: FAIL (no tool events)

- [ ] **Step 3: Implement**

Add to `coding_orchestrator.py` imports: `import time`, `import uuid`, and `from services.orchestrator import events` (if not already imported).

In `react_execute`, inside the loop where each tool call is dispatched (the `for tc in tool_calls:` body, before executing `run_bash` / `call_skill_tool`), wrap the dispatch. For each tool call compute the kind and emit start/done around the existing handler:

```python
                    _tool_id = uuid.uuid4().hex[:12]
                    _kind = "skill" if name == "call_skill_tool" else "tool"
                    if name in ("run_bash", "call_skill_tool"):
                        await events.emit(
                            "tool.start",
                            tool_id=_tool_id,
                            name=(args.get("skill") if name == "call_skill_tool" else "run_bash"),
                            kind=_kind,
                            args=args,
                            reasoning_why=events.extract_reasoning(r),
                        )
                        _t0 = time.monotonic()
                    # ... existing dispatch produces `content` (the observation string) ...
                    if name in ("run_bash", "call_skill_tool"):
                        await events.emit(
                            "tool.done",
                            tool_id=_tool_id,
                            status="done",
                            summary=str(content)[:200],
                            result=content,
                            duration_ms=int((time.monotonic() - _t0) * 1000),
                        )
```

Place the `tool.start` emit immediately before the existing branch that handles the call, and the `tool.done` emit immediately after `content` is assigned for that branch (before the `messages.append({"role": "tool", ...})`). Also emit a `reasoning` event once per assistant turn that has `reasoning_content`, right after `msg`/`r` is obtained:

```python
                _turn_reasoning = events.extract_reasoning(r)
                if _turn_reasoning:
                    await events.emit(
                        "reasoning", node="execute",
                        summary=events.reasoning_summary(_turn_reasoning),
                        text=_turn_reasoning,
                    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_coding_orchestrator.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/coding_orchestrator.py tests/services/orchestrator/test_coding_orchestrator.py
git commit -m "feat(events): ReAct executor emits tool.start/tool.done + reasoning"
```

---

## Task 5: Reference consumer + live end-to-end verification

**Files:**
- Create: `services/cli/event_stream.py`
- Test: `tests/services/cli/test_event_stream.py`

**Interfaces:**
- Produces:
  - `async def tail_events(redis_url: str, task_id: str, *, last_id: str = "0", block_ms: int = 5000)` — async generator yielding decoded event dicts via `XREAD BLOCK` on `labmate:events:<task_id>`, resuming from `last_id` (for reconnect/replay).
- Consumes: `events.EVENTS_STREAM_PREFIX`.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/cli/test_event_stream.py
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from services.cli import event_stream


@pytest.mark.asyncio
async def test_tail_events_decodes_and_advances(monkeypatch):
    # XREAD returns one batch then empty (stop)
    batch = [["labmate:events:t1", [("1-0", {"event": json.dumps({"type": "tool.start", "seq": 1})})]]]
    calls = [batch, []]
    fake = MagicMock()
    fake.xread = AsyncMock(side_effect=lambda *a, **k: calls.pop(0) if calls else [])
    monkeypatch.setattr(event_stream.aioredis, "from_url", lambda *a, **k: fake)

    got = []
    async for evt in event_stream.tail_events("redis://x", "t1"):
        got.append(evt)
        if evt.get("type") == "tool.start":
            break
    assert got[0]["type"] == "tool.start" and got[0]["seq"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python -m pytest tests/services/cli/test_event_stream.py -q -p no:cacheprovider`
Expected: FAIL (`ModuleNotFoundError` for `services.cli.event_stream`)

- [ ] **Step 3: Implement**

```python
# services/cli/event_stream.py
"""Reference consumer for the agent event stream (labmate:events:<task_id>).

Used by the CLI to render tool selection / lifecycle / reasoning live, and by
tests. A WebSocket gateway for the frontend would consume this same stream the
same way (XREAD BLOCK), then relay frames — no orchestrator change needed.
"""
from __future__ import annotations

import json
from typing import AsyncGenerator

import redis.asyncio as aioredis

from services.orchestrator.events import EVENTS_STREAM_PREFIX


async def tail_events(
    redis_url: str, task_id: str, *, last_id: str = "0", block_ms: int = 5000
) -> AsyncGenerator[dict, None]:
    """Yield decoded events for a task, resuming from last_id (replay-friendly)."""
    r = aioredis.from_url(redis_url, decode_responses=True)
    stream = f"{EVENTS_STREAM_PREFIX}{task_id}"
    cur = last_id
    while True:
        resp = await r.xread({stream: cur}, count=50, block=block_ms)
        if not resp:
            continue
        for _stream, entries in resp:
            for entry_id, fields in entries:
                cur = entry_id
                try:
                    yield json.loads(fields["event"])
                except (KeyError, json.JSONDecodeError):
                    continue
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. python -m pytest tests/services/cli/test_event_stream.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: Run the full affected suites (no regressions)**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator tests/services/cli tests/services/skill_runner tests/services/skill_worker -q -p no:cacheprovider`
Expected: PASS (all)

- [ ] **Step 6: Live end-to-end verification (requires running stack)**

Start a background subscriber, push a skill-shaped goal, confirm the three event kinds appear in order:

```bash
# terminal A — subscribe BEFORE pushing (use a known task_id)
source infrastructure/local/local.env
TID="evt-$(date +%s)"
PYTHONPATH=. python - "$TID" <<'PY' &
import asyncio, sys
from services.cli.event_stream import tail_events
async def main():
    async for e in tail_events("redis://localhost:6379/0", sys.argv[1]):
        t=e["type"]
        if t=="tool.start": print("TOOL.START", e["name"], "why:", (e.get("reasoning_why") or "")[:60])
        elif t=="tool.done": print("TOOL.DONE", e.get("status"), e.get("duration_ms"),"ms")
        elif t=="reasoning": print("REASONING", e.get("node"), (e.get("summary") or "")[:60])
        elif t=="turn.done": print("TURN.DONE", e.get("status")); break
asyncio.run(main())
PY
sleep 1
# terminal A (same shell) — push the goal with that exact task_id
redis-cli XADD labmate:goals '*' payload \
  "{\"task_id\":\"$TID\",\"task\":\"Parse the PDF at /tmp/smoke/sample.pdf into structured markdown\",\"session_id\":\"$TID\"}" >/dev/null
wait
```

Expected output (order/wording may vary, but all three must appear):
```
REASONING route <why pdf-parse fits>
TOOL.START pdf-parse why: <reasoning>
TOOL.DONE done <N> ms
TURN.DONE complete
```

- [ ] **Step 7: Commit**

```bash
git add services/cli/event_stream.py tests/services/cli/test_event_stream.py
git commit -m "feat(events): reference event-stream consumer + live e2e verification"
```

---

## Self-Review

**1. Spec coverage (the 3 asks):**
- "Which tools/skills were selected" → `tool.start` (`name`, `kind`, `args`) — Tasks 3 (skill path) & 4 (ReAct tools). ✓
- "When running and when finished" → `tool.start` then `tool.done` (`status`, `duration_ms`) — Tasks 3 & 4. ✓
- "Reasoning of the model in the tool call (debug)" → `reasoning` events + `tool.start.reasoning_why` from `reasoning_content` — Tasks 1 (capture), 3, 4. ✓
- Transport-agnostic → Redis Stream + `tail_events` consumed identically by CLI now and a WS gateway later — Tasks 1 & 5. ✓

**2. Placeholder scan:** No TBD/TODO; every code step has real code; commands have expected output. ✓

**3. Type consistency:** `EventEmitter.emit(type, **fields)`, module `emit(...)`, `extract_reasoning`, `reasoning_summary`, `current_emitter`, `EVENTS_STREAM_PREFIX`, `tail_events` are referenced with identical names across tasks. `tool_id` correlates `tool.start`↔`tool.done` in both Task 3 and Task 4. ✓

**Out of scope (intentionally — not in this plan):** WebSocket gateway, auth, artifacts, context-window accounting, `agent.status`, modes, `answer.delta` token streaming. These are the rest of `FRONTEND_SPEC.md` and can layer on later without changing the event channel.

## Execution Handoff

Two execution options:
1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks.
2. **Inline Execution** — execute tasks here with checkpoints.
