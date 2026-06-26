# Live Interrupt Steering (+ wire the missing cancel-check) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user steer or cancel a running agent turn mid-flight: a steer message typed during a turn is injected as a genuine out-of-band user instruction on the next ReAct turn (the model adjusts), and a cancel halts the loop with an honest partial summary — wiring in the cancel-check that the loop currently never performs.

**Architecture:** Three layers. (1) `events.py` gains a Redis steer channel (`labmate:steer:<task_id>`) mirroring the existing `CANCEL_PREFIX` helpers, consumed exactly once via `GETDEL`. (2) `_run_react_loop` checks, at the TOP of every turn, the cancel flag (break with partial summary) and drains the steer slot (inject a marked out-of-band user message onto the last tool message, repairing role alternation). The task_id is obtained from the already-active `EventEmitter` context-var (the same mechanism `local_tools._current_task_id()` uses) — **no signature change to `react_execute`/`_run_react_loop`**, so the dozens of existing call-sites keep working. (3) `ws_gateway` gains a `steer` client frame that writes to the steer channel, mirroring the existing `cancel` path, plus system-prompt guidance explaining the out-of-band marker is real.

**Tech Stack:** Python 3, `redis.asyncio` (live) / `fakeredis.aioredis` (tests), pytest + pytest-asyncio, pytest-bdd (`@mocked` features + `fake_model`), FastAPI WebSocket (ws_gateway).

## Global Constraints

- **stdout is sacred in MCP / orchestrator** — never `print()` / `console.log()`; log to stderr via `logging`. (CLAUDE.md rule 1)
- **Every `litellm`/model call sets `extra_body={"thinking_budget_tokens": N}` and `api_key="not-needed"`.** This feature adds NO new model calls — it only mutates the `messages` list passed to the existing `acompletion_with_failover` call in `_run_react_loop`. Do not add a model call. (CLAUDE.md rule 6)
- **Redis = `redis.asyncio`; pin `redis>=5.0,<6`.** Use `GETDEL` for atomic consume-once. (CLAUDE.md rule 5)
- **Best-effort emission / signalling never breaks a task** — every Redis read in the loop path is wrapped so a Redis failure degrades to "no steer / not cancelled", never an exception that kills the turn. (events.py module docstring)
- **Additive + regression-safe** — with NO steer and NO cancel written, `_run_react_loop` must behave byte-identically to today. Every existing test in `tests/services/orchestrator/test_coding_orchestrator.py` and the `*_bdd.py` loop tests must still pass unchanged.
- **File naming:** Python `snake_case.py`, classes `PascalCase`, functions `snake_case`. (CLAUDE.md)
- **DEPENDENCY (sibling plan):** the steer injection calls `services.orchestrator.message_repair.sanitize_messages` to keep role alternation valid. That module does **not exist yet** (sibling plan `message_repair.py`). The wire-in MUST import it lazily and degrade gracefully (a local identity fallback) when absent, so this plan lands and passes on its own.
- **Concurrent-edit warning:** another workflow is editing `services/orchestrator/coding_orchestrator.py`. **Anchor on structure, not line numbers.** The injection point is the TOP of each iteration of the `while True:` loop inside `_run_react_loop` — specifically the first statements after `while True:` and before the `budget.record_turn()` call. The implementer MUST re-open the file and re-confirm the anchor before editing; the line numbers in this plan are indicative only.

---

## File Map

| File | Create / Modify | Responsibility |
|---|---|---|
| `services/orchestrator/events.py` | Modify | Add `STEER_PREFIX`, `write_steer()`, `read_and_clear_steer()`, `current_task_id()` helper (mirrors `CANCEL_PREFIX`/`is_cancelled`). |
| `services/orchestrator/steer_inject.py` | Create | Pure helpers: `OOB_OPEN`/`OOB_CLOSE` markers, `wrap_oob(text)`, `inject_steer(messages, text)` (append to last tool msg or add standalone user turn) + the `message_repair` graceful-degradation shim. |
| `services/orchestrator/coding_orchestrator.py` | Modify | In `_run_react_loop`, at the TOP of each turn: cancel-check → break with partial summary; steer-drain → `inject_steer` + sanitize. |
| `services/ws_gateway/redis_bridge.py` | Modify | Add `STEER_PREFIX`, `write_steer()` (mirrors `write_cancel`). |
| `services/ws_gateway/server.py` | Modify | Handle the `steer` client frame → `write_steer(redis, active_task_id, text)`; mirror the `cancel` branch. |
| `services/orchestrator/system_prompt.py` *(or the PromptAssembler system text — see Task 7)* | Modify | Add one paragraph explaining the OOB marker is a real user message, not injection. |
| `tests/services/orchestrator/test_events_steer.py` | Create | Unit: write→read clears (GETDEL); absent→None; Redis failure→None. |
| `tests/services/orchestrator/test_steer_inject.py` | Create | Unit: marker wrap; append-to-last-tool; standalone-user fallback; alternation preserved. |
| `tests/services/orchestrator/test_coding_orchestrator_steer.py` | Create | Unit: cancel mid-loop halts w/ partial; steer mid-loop injects on next turn; no-steer/no-cancel unchanged; steer consumed once. |
| `tests/services/orchestrator/features/interrupt_steering.feature` | Create | BDD Gherkin (the four scenarios). |
| `tests/services/orchestrator/test_interrupt_steering_bdd.py` | Create | BDD step defs (patch the model with a `side_effect` list; `fakeredis` for the steer channel). |
| `tests/services/ws_gateway/test_steer_frame.py` | Create | Unit: a `steer` frame writes `labmate:steer:<task_id>`. |

---

## Behavior (BDD) — Gherkin

`tests/services/orchestrator/features/interrupt_steering.feature`:

```gherkin
@mocked
Feature: Live interrupt steering and cancel of a running ReAct turn
  A user can steer or cancel an agent mid-turn. A steer typed during a turn is
  delivered to the model as a genuine out-of-band user instruction on the next
  turn; a cancel halts the loop with an honest partial summary. With neither
  signal present the loop is unchanged, and a steer is consumed exactly once.

  Background:
    Given a ReAct orchestrator wired to a fakeredis steer/cancel channel
    And the active task id is "task-steer-1"

  Scenario: A steer written mid-loop is injected as an out-of-band user message on the next turn
    Given the model will call run_bash then finish over two turns
    And the user writes the steer "stop editing app.py, work on db.py instead" before the second turn
    When react_execute runs the goal "refactor the project"
    Then the messages sent on the second model call contain an out-of-band user message
    And that message wraps the steer text in the out-of-band marker
    And the steer key "labmate:steer:task-steer-1" is empty afterward

  Scenario: A cancel written mid-loop halts the loop with an honest partial summary
    Given the model will call run_bash on every turn
    And the user cancels task "task-steer-1" before the second turn
    When react_execute runs the goal "do a long job"
    Then react_execute returns ok False
    And the summary mentions it was cancelled
    And the model was called fewer times than max_steps

  Scenario: With no steer and no cancel the loop is unchanged
    Given the model will call finish on the first turn with summary "all done"
    When react_execute runs the goal "trivial task"
    Then react_execute returns ok True
    And the summary is "all done"
    And no out-of-band user message was injected

  Scenario: A steer is consumed exactly once
    Given the model will call run_bash on three turns then finish
    And the user writes the steer "use the staging database" before the second turn
    When react_execute runs the goal "multi-step job"
    Then exactly one model call carried an out-of-band user message
    And the steer key "labmate:steer:task-steer-1" is empty afterward
```

---

## Task 1: Steer channel helpers in `events.py`

**Files:**
- Modify: `services/orchestrator/events.py` (append after the `CANCEL_PREFIX` / `is_cancelled` block near EOF, ~line 110)
- Test: `tests/services/orchestrator/test_events_steer.py` (create)

**Interfaces:**
- Consumes: nothing new (uses `redis.asyncio.Redis`, already imported as `aioredis`).
- Produces:
  - `STEER_PREFIX = "labmate:steer:"`
  - `async def write_steer(redis: aioredis.Redis, task_id: str, text: str) -> None`
  - `async def read_and_clear_steer(redis: aioredis.Redis, task_id: str) -> str | None` — atomic GETDEL; returns the text once then None; best-effort (returns None on Redis error).
  - `def current_task_id() -> str | None` — returns the active EventEmitter's `_task_id`, or None when no emitter is set (mirrors `local_tools._current_task_id` but returns None instead of raising, so the loop degrades cleanly in unit tests).

- [ ] **Step 1: Write the failing tests**

`tests/services/orchestrator/test_events_steer.py`:

```python
import pytest
import fakeredis.aioredis
from unittest.mock import AsyncMock, MagicMock

from services.orchestrator import events
from services.orchestrator.events import (
    STEER_PREFIX,
    write_steer,
    read_and_clear_steer,
    current_task_id,
)


@pytest.mark.asyncio
async def test_write_then_read_returns_text_and_clears():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await write_steer(r, "t-1", "work on db.py instead")
    first = await read_and_clear_steer(r, "t-1")
    assert first == "work on db.py instead"
    # GETDEL semantics: a second read sees nothing.
    assert await read_and_clear_steer(r, "t-1") is None
    assert await r.exists(f"{STEER_PREFIX}t-1") == 0


@pytest.mark.asyncio
async def test_read_absent_is_none():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    assert await read_and_clear_steer(r, "missing") is None


@pytest.mark.asyncio
async def test_read_swallows_redis_error():
    r = MagicMock()
    r.getdel = AsyncMock(side_effect=RuntimeError("redis down"))
    assert await read_and_clear_steer(r, "t-err") is None


@pytest.mark.asyncio
async def test_current_task_id_from_active_emitter_else_none():
    assert current_task_id() is None
    em = events.EventEmitter(MagicMock(), "task-abc")
    token = events.current_emitter.set(em)
    try:
        assert current_task_id() == "task-abc"
    finally:
        events.current_emitter.reset(token)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_events_steer.py -v`
Expected: FAIL — `ImportError: cannot import name 'write_steer' from 'services.orchestrator.events'`

- [ ] **Step 3: Implement the helpers**

Append to `services/orchestrator/events.py`, immediately after the existing `is_cancelled` function (which ends near line 118):

```python
STEER_PREFIX = "labmate:steer:"
STEER_TTL = 300  # seconds — a stale steer self-expires if never drained


async def write_steer(redis: aioredis.Redis, task_id: str, text: str) -> None:
    """Queue an out-of-band steer message for a running task (best-effort).

    Overwrites any pending-but-undrained steer (latest user instruction wins).
    """
    try:
        await redis.set(f"{STEER_PREFIX}{task_id}", text, ex=STEER_TTL)
    except Exception as exc:  # never let signalling break the caller
        _log.warning("write_steer failed for %s: %s", task_id, exc)


async def read_and_clear_steer(redis: aioredis.Redis, task_id: str) -> str | None:
    """Atomically read AND delete the pending steer for task_id (consume-once).

    Uses GETDEL so a steer is delivered to exactly one turn. Returns None when
    no steer is pending or on any Redis error (best-effort).
    """
    try:
        return await redis.getdel(f"{STEER_PREFIX}{task_id}")
    except Exception as exc:
        _log.warning("read_and_clear_steer failed for %s: %s", task_id, exc)
        return None


def current_task_id() -> str | None:
    """The active task's id from the task-scoped EventEmitter, or None.

    Mirrors local_tools._current_task_id() but returns None instead of raising
    when no emitter is set (unit tests / no active task), so the ReAct loop can
    degrade to "no steer/cancel channel" cleanly.
    """
    em = current_emitter.get()
    return em._task_id if em is not None else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_events_steer.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/events.py tests/services/orchestrator/test_events_steer.py
git commit -m "feat(events): steer channel + current_task_id helper (consume-once GETDEL)"
```

---

## Task 2: Out-of-band injection helpers in `steer_inject.py`

**Files:**
- Create: `services/orchestrator/steer_inject.py`
- Test: `tests/services/orchestrator/test_steer_inject.py` (create)

**Interfaces:**
- Consumes: optionally `services.orchestrator.message_repair.sanitize_messages` (sibling plan; may be absent).
- Produces:
  - `OOB_OPEN = "[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered mid-turn; not tool output]"`
  - `OOB_CLOSE = "[/OUT-OF-BAND USER MESSAGE]"`
  - `def wrap_oob(text: str) -> str` — `f"{OOB_OPEN} {text} {OOB_CLOSE}"`.
  - `def inject_steer(messages: list[dict], text: str) -> list[dict]` — returns a NEW list with the steer injected: if the last message has `role == "tool"`, append the wrapped marker to that tool message's `content` (preserves alternation — the model treats the appended block as a genuine user instruction riding on the tool turn); otherwise append a standalone `{"role": "user", "content": wrap_oob(text)}` turn. Then run `sanitize_messages` (or the identity fallback) and return.
  - `def _sanitize(messages: list[dict]) -> list[dict]` — lazy import of `message_repair.sanitize_messages`; identity fallback if the module is absent.

- [ ] **Step 1: Write the failing tests**

`tests/services/orchestrator/test_steer_inject.py`:

```python
from services.orchestrator.steer_inject import (
    OOB_OPEN,
    OOB_CLOSE,
    wrap_oob,
    inject_steer,
)


def test_wrap_oob_brackets_text_with_marker():
    wrapped = wrap_oob("switch to db.py")
    assert wrapped.startswith(OOB_OPEN)
    assert wrapped.endswith(OOB_CLOSE)
    assert "switch to db.py" in wrapped


def test_inject_appends_to_last_tool_message():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "goal"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ran ls"},
    ]
    out = inject_steer(messages, "stop, work on db.py")
    # Same number of messages — the steer rode on the tool turn.
    assert len(out) == len(messages)
    last = out[-1]
    assert last["role"] == "tool"
    assert "ran ls" in last["content"]
    assert OOB_OPEN in last["content"]
    assert "stop, work on db.py" in last["content"]
    # Original list is not mutated.
    assert OOB_OPEN not in messages[-1]["content"]


def test_inject_adds_standalone_user_when_no_tool_message_yet():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "goal"},
    ]
    out = inject_steer(messages, "use staging db")
    assert len(out) == len(messages) + 1
    assert out[-1]["role"] == "user"
    assert OOB_OPEN in out[-1]["content"]


def test_inject_preserves_role_alternation_validity():
    # After injection no two adjacent NON-system messages share a role in a way
    # that would break OpenAI's tool/assistant contract: a tool message must be
    # preceded by an assistant with tool_calls; a standalone user is fine after
    # a user/assistant. We assert there is never an orphan tool message.
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "goal"},
    ]
    out = inject_steer(messages, "x")
    for i, m in enumerate(out):
        if m["role"] == "tool":
            assert i > 0 and out[i - 1]["role"] == "assistant"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_steer_inject.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.orchestrator.steer_inject'`

- [ ] **Step 3: Implement `steer_inject.py`**

`services/orchestrator/steer_inject.py`:

```python
"""Out-of-band steer injection: turn a mid-turn user steer into a genuine
user instruction inside the ReAct message list, preserving role alternation.

The injected text is wrapped in an explicit marker so the model treats it as a
real, mid-turn user message (the system prompt explains the marker is genuine,
NOT a prompt-injection attack). Injecting onto the LAST tool message keeps the
assistant/tool/user alternation valid (the steer rides on the tool turn the
model just produced); when no tool message exists yet the steer becomes a
standalone user turn after the goal.
"""
from __future__ import annotations

import copy

OOB_OPEN = (
    "[OUT-OF-BAND USER MESSAGE — a direct message from the user, "
    "delivered mid-turn; not tool output]"
)
OOB_CLOSE = "[/OUT-OF-BAND USER MESSAGE]"


def wrap_oob(text: str) -> str:
    """Wrap steer text in the out-of-band marker."""
    return f"{OOB_OPEN} {text} {OOB_CLOSE}"


def _sanitize(messages: list[dict]) -> list[dict]:
    """Repair role alternation via the sibling message_repair module.

    DEPENDENCY: services.orchestrator.message_repair.sanitize_messages (sibling
    plan). If that module is not present yet, degrade to identity — injection
    onto the last tool message already preserves a valid shape, so a missing
    repair pass is safe, not a correctness hole.
    """
    try:
        from services.orchestrator.message_repair import sanitize_messages
    except Exception:
        return messages
    try:
        return sanitize_messages(messages)
    except Exception:
        return messages


def inject_steer(messages: list[dict], text: str) -> list[dict]:
    """Return a NEW message list with the steer injected as a marked OOB user
    instruction. Appends to the last tool message when present (preserving
    alternation); otherwise adds a standalone user turn.
    """
    out = copy.deepcopy(messages)
    wrapped = wrap_oob(text)
    if out and out[-1].get("role") == "tool":
        existing = out[-1].get("content") or ""
        out[-1]["content"] = f"{existing}\n\n{wrapped}" if existing else wrapped
    else:
        out.append({"role": "user", "content": wrapped})
    return _sanitize(out)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_steer_inject.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/steer_inject.py tests/services/orchestrator/test_steer_inject.py
git commit -m "feat(orchestrator): out-of-band steer injection helpers (marker + alternation-safe)"
```

---

## Task 3: Wire cancel-check + steer-drain into `_run_react_loop`

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py` — function `_run_react_loop`, TOP of the `while True:` body. **Re-confirm the anchor before editing (concurrent edits).**
- Test: `tests/services/orchestrator/test_coding_orchestrator_steer.py` (create)

**Interfaces:**
- Consumes: `events.current_task_id`, `events.is_cancelled`, `events.read_and_clear_steer` (Task 1); `steer_inject.inject_steer` (Task 2); `self.redis` (already an `AsyncOrchestrator` attribute, set in `main.py`).
- Produces: no new public symbol — behavior change inside `_run_react_loop` only.

**Anchor (structure, not line number):** Inside `_run_react_loop`, the loop body begins with:

```python
        while True:
            # Hard absolute ceiling (prevents infinite loops of distinct cheap reads).
            if not budget.record_turn():
                return {"ok": False, "summary": "absolute turn limit exceeded"}
```

The new block goes **immediately after `while True:` and BEFORE `if not budget.record_turn():`** — so cancel/steer are checked at the very top of every turn, before any budget accounting or model call.

- [ ] **Step 1: Write the failing tests**

`tests/services/orchestrator/test_coding_orchestrator_steer.py`:

```python
import json
import pytest
import fakeredis.aioredis
from unittest.mock import AsyncMock, MagicMock, patch

from services.orchestrator.coding_orchestrator import AsyncOrchestrator
from services.orchestrator import events
from services.orchestrator.steer_inject import OOB_OPEN


def _bash_then_finish():
    """Two model responses: turn 1 calls run_bash; turn 2 calls finish."""
    def _mk_bash():
        tc = MagicMock()
        tc.id = "c1"
        tc.function = MagicMock()
        tc.function.name = "run_bash"
        tc.function.arguments = json.dumps({"command": "ls"})
        msg = MagicMock()
        msg.content = None
        msg.tool_calls = [tc]
        msg.reasoning_content = ""
        msg.model_dump = lambda: {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "run_bash", "arguments": "{}"}}],
        }
        return MagicMock(choices=[MagicMock(message=msg)])

    def _mk_finish():
        tc = MagicMock()
        tc.id = "c2"
        tc.function = MagicMock()
        tc.function.name = "finish"
        tc.function.arguments = json.dumps({"summary": "done"})
        msg = MagicMock()
        msg.content = None
        msg.tool_calls = [tc]
        msg.reasoning_content = ""
        msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
        return MagicMock(choices=[MagicMock(message=msg)])

    return [_mk_bash(), _mk_finish()]


def _always_bash():
    def _mk():
        tc = MagicMock()
        tc.id = "c"
        tc.function = MagicMock()
        tc.function.name = "run_bash"
        tc.function.arguments = json.dumps({"command": "ls"})
        msg = MagicMock()
        msg.content = None
        msg.tool_calls = [tc]
        msg.reasoning_content = ""
        msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
        return MagicMock(choices=[MagicMock(message=msg)])
    return _mk


@pytest.fixture
def orch_with_redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    orch = AsyncOrchestrator(skill_router=None, mcp=AsyncMock(), workspace="/tmp", max_steps=6)
    bash_result = MagicMock()
    bash_result.content = [MagicMock(text="files")]
    bash_result.isError = False
    orch.mcp.call_tool = AsyncMock(return_value=bash_result)
    orch.redis = r
    return orch, r


async def _with_task(task_id, coro_fn):
    """Run coro_fn() with an active EventEmitter so current_task_id() works."""
    em = events.EventEmitter(MagicMock(), task_id)
    em.emit = AsyncMock()  # swallow event emission in unit tests
    token = events.current_emitter.set(em)
    try:
        return await coro_fn()
    finally:
        events.current_emitter.reset(token)


@pytest.mark.asyncio
async def test_steer_injected_on_next_turn(orch_with_redis):
    orch, r = orch_with_redis
    captured = []

    async def _capture(*a, **k):
        # Record the messages seen on each model call, then return the scripted resp.
        captured.append([dict(m) for m in k["messages"]])
        return _capture.responses.pop(0)
    _capture.responses = _bash_then_finish()

    await events.write_steer(r, "t-steer", "work on db.py instead")

    async def _run():
        with patch("services.orchestrator.coding_orchestrator.acompletion_with_failover",
                   new=AsyncMock(side_effect=_capture)):
            return await orch._run_react_loop("refactor", 6)

    await _with_task("t-steer", _run)

    # The SECOND model call must carry the out-of-band user message.
    assert len(captured) == 2
    second_blob = json.dumps(captured[1])
    assert OOB_OPEN in second_blob
    assert "work on db.py instead" in second_blob
    # Consumed exactly once — the key is gone.
    assert await r.exists("labmate:steer:t-steer") == 0


@pytest.mark.asyncio
async def test_cancel_halts_with_partial_summary(orch_with_redis):
    orch, r = orch_with_redis
    calls = {"n": 0}

    async def _count(*a, **k):
        calls["n"] += 1
        # Cancel arrives after the first model call, before the second turn-top check.
        if calls["n"] == 1:
            await events.write_steer  # no-op ref; keep import warm
        return _always_bash()()

    await r.set("labmate:cancel:t-cancel", "1", ex=60)

    async def _run():
        with patch("services.orchestrator.coding_orchestrator.acompletion_with_failover",
                   new=AsyncMock(side_effect=_count)):
            return await orch._run_react_loop("long job", 6)

    result = await _with_task("t-cancel", _run)
    assert result["ok"] is False
    assert "cancel" in result["summary"].lower()
    # Halted at the very first turn-top check → model never called.
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_no_steer_no_cancel_unchanged(orch_with_redis):
    orch, r = orch_with_redis

    async def _finish(*a, **k):
        tc = MagicMock(); tc.id = "c"; tc.function = MagicMock()
        tc.function.name = "finish"
        tc.function.arguments = json.dumps({"summary": "all done"})
        msg = MagicMock(); msg.content = None; msg.tool_calls = [tc]
        msg.reasoning_content = ""
        msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
        return MagicMock(choices=[MagicMock(message=msg)])

    async def _run():
        with patch("services.orchestrator.coding_orchestrator.acompletion_with_failover",
                   new=AsyncMock(side_effect=_finish)):
            return await orch._run_react_loop("trivial", 6)

    result = await _with_task("t-plain", _run)
    assert result["ok"] is True
    assert result["summary"] == "all done"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_coding_orchestrator_steer.py -v`
Expected: FAIL — steer not injected (no `OOB_OPEN` in second call) and cancel not honored (`calls["n"] != 0`), because `_run_react_loop` does not yet check cancel/steer.

- [ ] **Step 3: Add the import**

At the top of `services/orchestrator/coding_orchestrator.py`, the line `from . import events` already exists. Add the steer-inject import beside the other local imports (e.g. directly after `from .iteration_budget import IterationBudget, CHEAP_TOOLS`):

```python
from .steer_inject import inject_steer
```

- [ ] **Step 4: Insert the turn-top cancel + steer block**

In `_run_react_loop`, insert this block as the FIRST statements inside `while True:`, before `if not budget.record_turn():`:

```python
            while True:
                # ── Live interrupt: cancel + steer (top of every turn) ──────────
                # task_id comes from the active EventEmitter (set per-task in
                # main._handle); None in unit tests with no emitter / no redis,
                # in which case both checks are skipped and the loop is unchanged.
                _task_id = events.current_task_id()
                if _task_id is not None and self.redis is not None:
                    # (1) Cancel — honest partial halt (this is the in-loop cancel
                    #     check that was previously MISSING entirely).
                    if await events.is_cancelled(self.redis, _task_id):
                        await events.emit("turn.cancelled", task_id=_task_id, steps=budget.used)
                        return {
                            "ok": False,
                            "summary": (
                                "cancelled by user mid-turn; partial progress only — "
                                "the requested work was not fully completed"
                            ),
                        }
                    # (2) Steer — drain the pending mid-turn user instruction and
                    #     inject it as a marked out-of-band user message on the LAST
                    #     tool message (or a standalone user turn if none yet), so
                    #     the next model call treats it as a genuine user steer.
                    _steer = await events.read_and_clear_steer(self.redis, _task_id)
                    if _steer:
                        messages = inject_steer(messages, _steer)
                        await events.emit("steer.injected", task_id=_task_id, text=_steer)

                # Hard absolute ceiling (prevents infinite loops of distinct cheap reads).
                if not budget.record_turn():
                    return {"ok": False, "summary": "absolute turn limit exceeded"}
```

(Everything below `if not budget.record_turn():` is unchanged.)

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_coding_orchestrator_steer.py -v`
Expected: PASS (3 passed)

- [ ] **Step 6: Run the full loop-mechanics regression suite (additive-safe check)**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_coding_orchestrator.py tests/services/orchestrator/test_tool_loop_detection_bdd.py tests/services/orchestrator/test_iteration_budget_bdd.py tests/services/orchestrator/test_run_tests_tool_bdd.py tests/services/orchestrator/test_prefix_cache_stability_bdd.py -q`
Expected: PASS — all existing loop tests still green (no emitter set in those tests → `current_task_id()` is None → cancel/steer skipped → byte-identical behavior).

- [ ] **Step 7: Commit**

```bash
git add services/orchestrator/coding_orchestrator.py tests/services/orchestrator/test_coding_orchestrator_steer.py
git commit -m "feat(orchestrator): wire in-loop cancel check + live steer injection at turn top"
```

---

## Task 4: ws_gateway steer channel writer in `redis_bridge.py`

**Files:**
- Modify: `services/ws_gateway/redis_bridge.py` (append after the `write_cancel` / `check_cancel` block at EOF)
- Test: `tests/services/ws_gateway/test_steer_frame.py` (create; this task tests the bridge writer; Task 5 adds the server frame)

**Interfaces:**
- Produces:
  - `STEER_PREFIX = "labmate:steer:"`
  - `STEER_TTL = 300`
  - `async def write_steer(redis: aioredis.Redis, task_id: str, text: str) -> None` — mirror of `write_cancel`; writes the steer key with TTL.

- [ ] **Step 1: Write the failing test**

`tests/services/ws_gateway/test_steer_frame.py`:

```python
import pytest
import fakeredis.aioredis

from services.ws_gateway.redis_bridge import write_steer, STEER_PREFIX


@pytest.mark.asyncio
async def test_write_steer_sets_key_with_text():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await write_steer(r, "task-9", "switch to db.py")
    assert await r.get(f"{STEER_PREFIX}task-9") == "switch to db.py"
    assert await r.ttl(f"{STEER_PREFIX}task-9") > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/ws_gateway/test_steer_frame.py -v`
Expected: FAIL — `ImportError: cannot import name 'write_steer' from 'services.ws_gateway.redis_bridge'`

- [ ] **Step 3: Implement the bridge writer**

Append to `services/ws_gateway/redis_bridge.py`, after `check_cancel` (EOF):

```python
STEER_PREFIX = "labmate:steer:"
STEER_TTL = 300  # seconds — a stale steer self-expires if the turn ends first


async def write_steer(redis: aioredis.Redis, task_id: str, text: str) -> None:
    """Queue an out-of-band steer message for a running task.

    The orchestrator drains this at the top of its next ReAct turn (GETDEL) and
    injects it as a marked user message. Latest write wins; TTL self-cleans.
    """
    await redis.set(f"{STEER_PREFIX}{task_id}", text, ex=STEER_TTL)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/ws_gateway/test_steer_frame.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add services/ws_gateway/redis_bridge.py tests/services/ws_gateway/test_steer_frame.py
git commit -m "feat(ws_gateway): write_steer redis-bridge helper (mirrors write_cancel)"
```

---

## Task 5: ws_gateway `steer` client frame in `server.py`

**Files:**
- Modify: `services/ws_gateway/server.py` — import `write_steer`; add a `steer` branch in `_ws_loop`'s message loop (mirror the `cancel` branch).
- Test: extend `tests/services/ws_gateway/test_steer_frame.py` with a loop-level test using a fake WebSocket.

**Interfaces:**
- Consumes: `write_steer` (Task 4); `active_task_id` (already tracked in `_ws_loop`).
- Produces: handling of `{"type": "steer", "text": "..."}` frames.

- [ ] **Step 1: Write the failing test**

Append to `tests/services/ws_gateway/test_steer_frame.py`:

```python
class _FakeWS:
    """Minimal WebSocket double: scripts receive_json, records send_json."""
    def __init__(self, incoming):
        self._incoming = list(incoming)
        self.sent = []

    async def receive_json(self):
        if not self._incoming:
            from fastapi import WebSocketDisconnect
            raise WebSocketDisconnect()
        return self._incoming.pop(0)

    async def send_json(self, data):
        self.sent.append(data)


@pytest.mark.asyncio
async def test_ws_loop_steer_frame_writes_steer_key(monkeypatch):
    import services.ws_gateway.server as server
    from services.ws_gateway.redis_bridge import STEER_PREFIX

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)

    # Auth handshake frame, then a send (to set active_task_id), then a steer.
    incoming = [
        {"type": "auth", "token": "tok"},
        {"type": "send", "text": "do a thing", "sessionId": "s1"},
        {"type": "steer", "text": "actually, use db.py"},
    ]
    ws = _FakeWS(incoming)

    # Stub auth to accept, boot to no-op, and _handle_send to bind a known task_id.
    class _Auth:
        def verify_token(self, t):
            return {"sub": "u1", "email": "u@x", "role": "user"}
    monkeypatch.setattr(server, "run_boot_sequence",
                         AsyncMock() if False else _noop_boot)

    async def _fake_handle_send(ws_, redis_, msg_, **kw):
        import asyncio as _a
        done = _a.get_event_loop().create_future()
        done.set_result(None)
        async def _relay():
            return None
        return "task-steered", _a.create_task(_relay())

    monkeypatch.setattr(server, "_handle_send", _fake_handle_send)

    from services.ws_gateway.sessions import InMemorySessionStore
    await server._ws_loop(ws, _Auth(), r, {}, InMemorySessionStore())

    assert await r.get(f"{STEER_PREFIX}task-steered") == "actually, use db.py"
```

Add this helper near the top of the test file (after imports):

```python
from unittest.mock import AsyncMock


async def _noop_boot(emit, checks, session_store=None):
    return None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/ws_gateway/test_steer_frame.py::test_ws_loop_steer_frame_writes_steer_key -v`
Expected: FAIL — no `steer` branch yet, so the frame falls to the `else: continue` and the key is never written (`assert ... == "actually, use db.py"` fails on `None`).

- [ ] **Step 3: Import `write_steer` in `server.py`**

Add `write_steer` to the existing `redis_bridge` import block:

```python
from services.ws_gateway.redis_bridge import (
    push_task,
    tail_task_events,
    translate_event,
    write_cancel,
    write_steer,
    write_tool_result,
)
```

- [ ] **Step 4: Add the `steer` branch in `_ws_loop`**

In `_ws_loop`'s `while True:` message dispatch, add a branch mirroring `cancel` — place it directly after the `elif mtype == "cancel":` block:

```python
        elif mtype == "steer":
            # Out-of-band steer: deliver a mid-turn user instruction to the
            # running task. The orchestrator drains it at the top of its next
            # ReAct turn and injects it as a marked user message — the relay
            # keeps streaming, the turn is NOT cancelled.
            steer_text = msg.get("text", "")
            if active_task_id is not None and steer_text:
                await write_steer(redis, active_task_id, steer_text)
            await ws.send_json({"type": "steer.ack", "taskId": active_task_id or ""})
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/ws_gateway/test_steer_frame.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Run the existing ws_gateway suite (regression)**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/ws_gateway/ -q`
Expected: PASS — no existing ws_gateway test broken.

- [ ] **Step 7: Commit**

```bash
git add services/ws_gateway/server.py tests/services/ws_gateway/test_steer_frame.py
git commit -m "feat(ws_gateway): steer client frame writes the steer channel (mirrors cancel)"
```

---

## Task 6: BDD scenarios for interrupt steering

**Files:**
- Create: `tests/services/orchestrator/features/interrupt_steering.feature` (the Gherkin in the "Behavior (BDD)" section above)
- Create: `tests/services/orchestrator/test_interrupt_steering_bdd.py`

**Interfaces:**
- Consumes: `AsyncOrchestrator._run_react_loop` (via `react_execute`), `events.write_steer`, `events.EventEmitter`/`current_emitter`, `steer_inject.OOB_OPEN`, `fakeredis`.
- Produces: nothing imported elsewhere.

- [ ] **Step 1: Create the feature file**

Write the full Gherkin from the "Behavior (BDD) — Gherkin" section above to `tests/services/orchestrator/features/interrupt_steering.feature`.

- [ ] **Step 2: Write the step defs (failing until wired)**

`tests/services/orchestrator/test_interrupt_steering_bdd.py`:

```python
from __future__ import annotations

import json
import pytest
import fakeredis.aioredis
from unittest.mock import AsyncMock, MagicMock, patch
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.coding_orchestrator import AsyncOrchestrator
from services.orchestrator import events
from services.orchestrator.steer_inject import OOB_OPEN
from tests.conftest import run_async

pytestmark = [pytest.mark.bdd, pytest.mark.mocked]

scenarios("features/interrupt_steering.feature")


def _tool_resp(name, args, call_id="c"):
    tc = MagicMock()
    tc.id = call_id
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = json.dumps(args)
    msg = MagicMock()
    msg.content = None
    msg.tool_calls = [tc]
    msg.reasoning_content = ""
    msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    return MagicMock(choices=[MagicMock(message=msg)])


@pytest.fixture
def ctx():
    return {
        "redis": fakeredis.aioredis.FakeRedis(decode_responses=True),
        "task_id": None,
        "responses": [],
        "steer_before_turn": None,
        "steer_text": None,
        "cancel_before_turn": None,
        "captured": [],
        "result": None,
    }


@given("a ReAct orchestrator wired to a fakeredis steer/cancel channel")
def _orch(ctx):
    orch = AsyncOrchestrator(skill_router=None, mcp=AsyncMock(), workspace="/tmp", max_steps=6)
    bash_result = MagicMock()
    bash_result.content = [MagicMock(text="files")]
    bash_result.isError = False
    orch.mcp.call_tool = AsyncMock(return_value=bash_result)
    orch.redis = ctx["redis"]
    ctx["orch"] = orch


@given(parsers.parse('the active task id is "{task_id}"'))
def _task_id(ctx, task_id):
    ctx["task_id"] = task_id


@given("the model will call run_bash then finish over two turns")
def _bash_then_finish(ctx):
    ctx["responses"] = [
        _tool_resp("run_bash", {"command": "ls"}, "c1"),
        _tool_resp("finish", {"summary": "done"}, "c2"),
    ]


@given("the model will call run_bash on every turn")
def _always_bash(ctx):
    ctx["responses"] = "ALWAYS_BASH"


@given("the model will call run_bash on three turns then finish")
def _three_bash_then_finish(ctx):
    ctx["responses"] = [
        _tool_resp("run_bash", {"command": "ls"}, "c1"),
        _tool_resp("run_bash", {"command": "pwd"}, "c2"),
        _tool_resp("run_bash", {"command": "whoami"}, "c3"),
        _tool_resp("finish", {"summary": "done"}, "c4"),
    ]


@given(parsers.parse('the model will call finish on the first turn with summary "{summary}"'))
def _finish_first(ctx, summary):
    ctx["responses"] = [_tool_resp("finish", {"summary": summary}, "c1")]


@given(parsers.parse('the user writes the steer "{text}" before the second turn'))
def _steer_before_second(ctx, text):
    ctx["steer_before_turn"] = 2
    ctx["steer_text"] = text


@given(parsers.parse('the user cancels task "{task_id}" before the second turn'))
def _cancel_before_second(ctx, task_id):
    ctx["cancel_before_turn"] = 2


@when(parsers.parse('react_execute runs the goal "{goal}"'))
def _run_goal(ctx, goal):
    orch = ctx["orch"]
    turn = {"n": 0}

    async def _model(*a, **k):
        turn["n"] += 1
        # Capture the messages this call saw (deep copy of the relevant shape).
        ctx["captured"].append(json.dumps(k["messages"], default=str))
        # Fire the scheduled steer/cancel BEFORE producing this turn's response,
        # so it is visible at the TOP of the NEXT turn.
        if ctx["steer_before_turn"] == turn["n"] + 1 and ctx["steer_text"]:
            await events.write_steer(ctx["redis"], ctx["task_id"], ctx["steer_text"])
        if ctx["cancel_before_turn"] == turn["n"] + 1:
            await ctx["redis"].set(f"labmate:cancel:{ctx['task_id']}", "1", ex=60)
        if ctx["responses"] == "ALWAYS_BASH":
            return _tool_resp("run_bash", {"command": "ls"})
        return ctx["responses"].pop(0)

    em = events.EventEmitter(ctx["redis"], ctx["task_id"])
    em.emit = AsyncMock()
    token = events.current_emitter.set(em)

    async def _go():
        with patch("services.orchestrator.coding_orchestrator.acompletion_with_failover",
                   new=AsyncMock(side_effect=_model)):
            return await orch.react_execute(goal)

    try:
        ctx["result"] = run_async(_go())
    finally:
        events.current_emitter.reset(token)


@then("the messages sent on the second model call contain an out-of-band user message")
def _second_has_oob(ctx):
    assert len(ctx["captured"]) >= 2
    assert OOB_OPEN in ctx["captured"][1]


@then("that message wraps the steer text in the out-of-band marker")
def _wraps_steer(ctx):
    assert ctx["steer_text"] in ctx["captured"][1]


@then(parsers.parse('the steer key "{key}" is empty afterward'))
def _steer_key_empty(ctx, key):
    assert run_async(ctx["redis"].exists(key)) == 0


@then("react_execute returns ok False")
def _ok_false(ctx):
    assert ctx["result"]["ok"] is False


@then("the summary mentions it was cancelled")
def _summary_cancel(ctx):
    assert "cancel" in ctx["result"]["summary"].lower()


@then("the model was called fewer times than max_steps")
def _fewer_calls(ctx):
    assert len(ctx["captured"]) < ctx["orch"].max_steps


@then("react_execute returns ok True")
def _ok_true(ctx):
    assert ctx["result"]["ok"] is True


@then(parsers.parse('the summary is "{summary}"'))
def _summary_is(ctx, summary):
    assert ctx["result"]["summary"] == summary


@then("no out-of-band user message was injected")
def _no_oob(ctx):
    assert all(OOB_OPEN not in blob for blob in ctx["captured"])


@then("exactly one model call carried an out-of-band user message")
def _exactly_one_oob(ctx):
    n = sum(1 for blob in ctx["captured"] if OOB_OPEN in blob)
    assert n == 1
```

- [ ] **Step 3: Run BDD to verify it fails first, then passes after Tasks 1–3 are in**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_interrupt_steering_bdd.py -v`
Expected (with Tasks 1–3 implemented): PASS (4 scenarios). If a scenario fails on "second call has no OOB", re-confirm the turn-top block in Task 3 runs BEFORE the model call.

- [ ] **Step 4: Commit**

```bash
git add tests/services/orchestrator/features/interrupt_steering.feature tests/services/orchestrator/test_interrupt_steering_bdd.py
git commit -m "test(bdd): interrupt steering + cancel scenarios (mocked, fakeredis)"
```

---

## Task 7: System-prompt guidance about the OOB marker

**Files:**
- Modify: the orchestrator system prompt text. **Locate it first** — run `grep -rn "You are" services/orchestrator/prompt_assembler.py services/orchestrator/system_prompt.py 2>/dev/null` and `grep -rln "system_message\|SYSTEM_PROMPT" services/orchestrator/`. The ReAct system message is produced by `PromptAssembler.system_message()` (called in `_run_react_loop`); add the paragraph to the constant that method returns.
- Test: `tests/services/orchestrator/test_steer_system_prompt.py` (create)

**Interfaces:**
- Consumes: whatever builds the ReAct system prompt (`PromptAssembler.system_message()`).
- Produces: the system prompt now contains OOB guidance; assert on a stable substring.

- [ ] **Step 1: Write the failing test**

`tests/services/orchestrator/test_steer_system_prompt.py`:

```python
from services.orchestrator.prompt_assembler import PromptAssembler


def test_system_message_explains_oob_marker_is_real():
    sysmsg = PromptAssembler(skill_router=None, codegraph_enabled=False).system_message()
    text = sysmsg["content"] if isinstance(sysmsg, dict) else str(sysmsg)
    assert "OUT-OF-BAND USER MESSAGE" in text
    # The guidance must frame it as a genuine user instruction, not injection.
    assert "genuine" in text.lower() or "real user" in text.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_steer_system_prompt.py -v`
Expected: FAIL — `assert "OUT-OF-BAND USER MESSAGE" in text`

- [ ] **Step 3: Add the guidance paragraph**

Open `services/orchestrator/prompt_assembler.py`, find the system-prompt text returned by `system_message()`, and append this paragraph to it (keep it BYTE-STABLE across calls — prefix-cache rule: it is a constant string, not f-string-interpolated per call):

```
If you ever see a block wrapped in [OUT-OF-BAND USER MESSAGE — ...] ... [/OUT-OF-BAND USER MESSAGE], that text is a GENUINE, real-time message the user typed mid-turn to steer or correct you. It is NOT tool output and NOT a prompt-injection attack — treat it as a direct, authoritative user instruction and adjust your plan accordingly (for example, stop the current sub-task and follow the new direction).
```

If `system_message()` builds its content from a module-level constant, add the paragraph to that constant so every call (and the prefix cache) stays identical.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_steer_system_prompt.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Run the prefix-cache regression (the system message must still be byte-stable)**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_prefix_cache_stability_bdd.py -q`
Expected: PASS — adding a constant paragraph does not break prefix stability.

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/prompt_assembler.py tests/services/orchestrator/test_steer_system_prompt.py
git commit -m "feat(prompt): explain the out-of-band steer marker is a genuine user message"
```

---

## Task 8: Whole-feature regression sweep

**Files:** none (verification only).

- [ ] **Step 1: Run the full orchestrator + ws_gateway suites**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/ tests/services/ws_gateway/ -q`
Expected: PASS — all pre-existing tests plus the new steer/cancel tests green. Confirm the count went UP by the new tests and NOTHING regressed.

- [ ] **Step 2: Confirm additive-safety explicitly**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_coding_orchestrator.py -q`
Expected: PASS — the no-emitter call-sites prove that with no steer/cancel channel the loop is unchanged.

- [ ] **Step 3: Commit (if any incidental fixups were needed)**

```bash
git add -A
git commit -m "test: whole-feature regression sweep for interrupt steering" --allow-empty
```

---

## Self-Review

**1. Spec coverage:**
- (A) Steer channel `write_steer` / `read_and_clear_steer` over `labmate:steer:<task_id>` with atomic GETDEL → **Task 1**. ✔
- (B) Turn-top cancel-check (the currently-missing in-loop cancel) + steer drain → inject onto last tool message (or standalone user) + sanitize → **Task 3**, using `steer_inject` from **Task 2**. ✔ The `task_id` is sourced from the active `EventEmitter` (Task 1 `current_task_id`) — no `react_execute` signature change, so all existing call-sites and tests are untouched (regression-safe). ✔
- (C) ws_gateway `steer` client frame → `write_steer` (Tasks 4–5); system-prompt OOB guidance → **Task 7**. ✔
- Steer helpers unit-tested with fakeredis (write→read clears; absent→None) → Task 1. ✔
- Injection preserves role alternation (asserted) → Task 2. ✔
- Cancel-in-loop tested → Task 3 + BDD Task 6. ✔
- Additive + regression-safe (no steer/cancel → identical) → Task 3 Step 6, Task 8. ✔
- Steer consumed exactly once → Task 1 (GETDEL) + BDD "exactly one OOB" scenario. ✔
- Full `.feature` with all four required scenarios (steer-injected-next-turn / cancel-halts-partial / unchanged / consumed-once) → "Behavior (BDD)" + Task 6. ✔

**2. Placeholder scan:** No "TBD/TODO/handle edge cases" — every code step shows real code. The ONE deliberate locate-it step is Task 7 Step "Files" (the system-prompt string's exact location must be confirmed in `prompt_assembler.py`); the grep command and the exact paragraph to add are both given, so it is actionable, not a placeholder.

**3. Type consistency:** `write_steer(redis, task_id, text)` and `read_and_clear_steer(redis, task_id) -> str | None` are identical across `events.py` (Task 1) and the ws_gateway writer (Task 4 — same name/signature, different module, intentional mirror of the existing `write_cancel` duplication between `events.is_cancelled` and `redis_bridge.check_cancel`). `inject_steer(messages, text) -> list[dict]`, `wrap_oob(text) -> str`, `OOB_OPEN`/`OOB_CLOSE` constants used consistently in Tasks 2, 3, 6. `current_task_id() -> str | None` used in Task 3. The Redis key `labmate:steer:<task_id>` is identical in `events.STEER_PREFIX` and `redis_bridge.STEER_PREFIX`.

**4. Dependency note (message_repair):** Task 2's `inject_steer` calls `services.orchestrator.message_repair.sanitize_messages` (sibling plan) via a try/except lazy import with an identity fallback. This plan **lands and passes with `message_repair.py` absent**; when the sibling module arrives, the repair pass activates automatically with no change here. The implementer should re-run Task 2 + Task 3 tests after the sibling lands to confirm the real sanitizer keeps them green.

**5. Concurrent-edit note:** `coding_orchestrator.py` is being edited by another workflow. Task 3 anchors on the *structure* (first statements inside `_run_react_loop`'s `while True:`, before `budget.record_turn()`) and on deriving `task_id` from the `EventEmitter` context-var — NOT on line numbers. The implementer MUST re-open the file and re-confirm the anchor before applying the edit.
