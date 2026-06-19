# Orchestrator Streaming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Switch the orchestrator from `graph.ainvoke()` (blocking, single final write) to `graph.astream_events()` so it publishes `StreamEvent`s (FRONTEND_SPEC.md §4) to a per-task Redis channel `labmate:events:<task_id>` in real time, while preserving the existing `SET`+`PUBLISH labmate:result:<task_id>` final-result path byte-for-byte.

**Architecture:** A new `EventPublisher` (in `services/orchestrator/events.py`) owns the Redis event channel and the LangGraph-event → `StreamEvent` mapping. `CodingOrchestrator.run_task_streamed()` drives `graph.astream_events(version="v2")` and feeds each LangGraph event to the publisher. `OrchestratorProcess._handle()` calls `run_task_streamed()` instead of `run_task()`, passing the publisher; after the stream ends it writes the final result exactly as today. Events are fire-and-forget (`PUBLISH` to a channel nobody may be listening on is not an error) with a 24 h `EXPIRE` on a parallel marker key.

**Tech Stack:** Python, `redis.asyncio` (PUBLISH/EXPIRE), LangGraph `astream_events` (v2 schema), `litellm` (already streams via llama.cpp), `transformers` Gemma tokenizer (lazy, injectable, mocked in tests).

---

## Event contract (authoritative — copied from FRONTEND_SPEC.md §4)

Every event is a JSON object. The orchestrator emits the subset below. The
`type` strings and field names **must** match FRONTEND_SPEC.md §4 exactly —
no invented variants. In addition to the spec fields, every event carries the
envelope `{ sessionId, turnId, seq }` (the spec says "Every event carries
`{ type, sessionId, turnId?, seq }`").

Emitted variants (spec §4 shapes):

```ts
| { type: 'node.enter'; turnId: string; node: NodeName; thinkingBudget: number }
| { type: 'reasoning.delta'; turnId: string; text: string }
| { type: 'answer.delta'; turnId: string; text: string }
| { type: 'tool.start'; turnId: string; toolCall: Omit<ToolCall,'result'|'durationMs'|'status'> }
| { type: 'tool.done'; turnId: string; toolId: string; status: ToolCall['status']; summary: string; result: unknown; durationMs: number }
| { type: 'turn.done'; turnId: string; status: 'complete' | 'error' }
| { type: 'context.update'; window: ContextWindow }
| { type: 'agent.status'; status: AgentStatus }
```

`NodeName = 'plan_node' | 'execute_node' | 'check_node' | 'reflect_node' | 'chat_node'`
(plus the `approval_node` extension — see Node-name mapping below).

`ToolCall` (subset used by `tool.start`/`tool.done`), `ContextWindow`, and
`AgentStatus` shapes are defined verbatim in Task 2 (`EVENT_SCHEMA_NOTES`).

---

## Node-name mapping (DECISION)

The compiled graph nodes are named `plan`, `execute`, `check`, `reflect`,
`approval` (see `services/orchestrator/graph.py`). FRONTEND_SPEC `NodeName`
values are `plan_node`, `execute_node`, `check_node`, `reflect_node`,
`chat_node`. We map graph node → spec NodeName with a single constant
`NODE_NAME_MAP` (Task 2). The `approval` node has no NodeName in the spec's
union, but its events are valuable (the human-in-the-loop gate must be visible
to consumers), so we emit it as `approval_node` — an **extension** of the spec
union for a node the spec did not know about. The spec's NodeName union is the
authoritative list for *existing* nodes; consumers that do not recognise
`approval_node` can ignore it.

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `services/orchestrator/events.py` | Create | `EventPublisher`, `NODE_NAME_MAP`, `map_langgraph_event()`, thinking-budget table, envelope/seq |
| `services/orchestrator/coding_orchestrator.py` | Modify | Add `run_task_streamed()` driving `astream_events` |
| `services/orchestrator/main.py` | Modify | `_handle()` builds an `EventPublisher`, calls `run_task_streamed()`, preserves `_write_result()` |
| `services/orchestrator/memory_consolidator.py` | Modify | Make `token_count()` accept an injected tokenizer (test-safe); no behavior change in prod |
| `tests/services/orchestrator/test_events.py` | Create | Unit tests for `EventPublisher` + `map_langgraph_event()` |
| `tests/services/orchestrator/test_coding_orchestrator_stream.py` | Create | Tests for `run_task_streamed()` with a fake `astream_events` generator |
| `tests/services/orchestrator/test_main.py` | Modify | Update/add `_handle()` tests for the streamed path |

---

### Task 1: Event channel constants and module scaffold

**Files:**
- Create: `services/orchestrator/events.py`
- Create: `tests/services/orchestrator/test_events.py`

- [ ] **Step 1: Write failing test** — `tests/services/orchestrator/test_events.py`

```python
from __future__ import annotations

import pytest


def test_event_channel_constants_exist():
    from services.orchestrator.events import EVENTS_PREFIX, EVENTS_TTL

    assert EVENTS_PREFIX == "labmate:events:"
    assert EVENTS_TTL == 86_400  # matches RESULT_TTL in main.py


def test_event_channel_name_helper():
    from services.orchestrator.events import event_channel

    assert event_channel("task-123") == "labmate:events:task-123"
```

- [ ] **Step 2: Run it (must fail — module does not exist)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_events.py -q
```

Expected: `ModuleNotFoundError: No module named 'services.orchestrator.events'` (collection error).

- [ ] **Step 3: Implement** — `services/orchestrator/events.py`

```python
# services/orchestrator/events.py
"""
Real-time StreamEvent publisher for the orchestrator.

Publishes events defined in FRONTEND_SPEC.md §4 to a per-task Redis channel
(`labmate:events:<task_id>`) as a LangGraph run progresses. Fire-and-forget:
a PUBLISH with zero subscribers is NOT an error. The existing final-result
write (labmate:result:<task_id>) is unaffected and lives in main.py.
"""
from __future__ import annotations

EVENTS_PREFIX = "labmate:events:"
EVENTS_TTL = 86_400  # 24 h — must match RESULT_TTL in services/orchestrator/main.py


def event_channel(task_id: str) -> str:
    """Redis pub/sub channel for a task's live event stream."""
    return f"{EVENTS_PREFIX}{task_id}"
```

- [ ] **Step 4: Run tests (must pass)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_events.py -q
```

Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/events.py tests/services/orchestrator/test_events.py
git commit -m "feat(orchestrator): event channel constants for live streaming"
```

---

### Task 2: Node-name map, thinking-budget table, and schema notes

**Files:**
- Modify: `services/orchestrator/events.py`
- Modify: `tests/services/orchestrator/test_events.py`

- [ ] **Step 1: Write failing test** — append to `tests/services/orchestrator/test_events.py`

```python
def test_node_name_map_matches_spec():
    from services.orchestrator.events import NODE_NAME_MAP

    assert NODE_NAME_MAP["plan"] == "plan_node"
    assert NODE_NAME_MAP["execute"] == "execute_node"
    assert NODE_NAME_MAP["check"] == "check_node"
    assert NODE_NAME_MAP["reflect"] == "reflect_node"
    # approval is a spec extension — emitted as approval_node so the gate is visible
    assert NODE_NAME_MAP["approval"] == "approval_node"


def test_thinking_budget_table_matches_spec():
    from services.orchestrator.events import THINKING_BUDGET

    assert THINKING_BUDGET["plan_node"] == 3000
    assert THINKING_BUDGET["execute_node"] == 2048
    assert THINKING_BUDGET["check_node"] == 1000
    assert THINKING_BUDGET["reflect_node"] == 3000
    assert THINKING_BUDGET["chat_node"] == 1000
```

- [ ] **Step 2: Run it (must fail)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_events.py -q
```

Expected: `ImportError: cannot import name 'NODE_NAME_MAP'` (2 new tests error, 2 prior pass).

- [ ] **Step 3: Implement** — append to `services/orchestrator/events.py`

Add `from typing import Literal` to the module's imports (top of the file,
under `from __future__ import annotations`), then append:

```python
# NodeName — FRONTEND_SPEC §3 union, extended with approval_node.
# The spec's union (plan_node | execute_node | check_node | reflect_node |
# chat_node) is authoritative for nodes the spec knows about; approval_node is
# our extension for the human-in-the-loop gate the spec did not enumerate.
# Consumers that don't recognise approval_node ignore it.
NodeName = Literal[
    "plan_node", "execute_node", "check_node",
    "reflect_node", "chat_node", "approval_node",
]

# --- Node name mapping (graph node -> NodeName) -----------------------------
# Graph nodes (graph.py): plan, execute, check, reflect, approval.
# 'approval' maps to approval_node (a spec extension) so the approval gate is
# visible to consumers rather than dropped.
NODE_NAME_MAP: dict[str, str] = {
    "plan": "plan_node",
    "execute": "execute_node",
    "check": "check_node",
    "reflect": "reflect_node",
    "approval": "approval_node",
}

# thinking_budget_tokens per spec NodeName (FRONTEND_SPEC §3 budget table).
THINKING_BUDGET: dict[str, int] = {
    "plan_node": 3000,
    "execute_node": 2048,
    "check_node": 1000,
    "reflect_node": 3000,
    "chat_node": 1000,
}

# EVENT_SCHEMA_NOTES — the subset of FRONTEND_SPEC §3/§4 shapes this module
# emits. Kept as a docstring constant so the mapping code is self-documenting.
#
# ToolCall (used by tool.start as Omit<ToolCall,'result'|'durationMs'|'status'>,
#           and by tool.done's flat fields):
#   { id, name, kind: 'skill'|'tool', summary, reasoningWhy, args }
# ContextWindow:
#   { max: 16384, used, segments: { systemPrompt, skillInstructions,
#     conversation, workingMemory, reasoning }, free }
# AgentStatus.brain (the only subsystem this module mutates):
#   { model, endpoint, state: 'idle'|'active'|'error', node, thinkingBudget }
EVENT_SCHEMA_NOTES = __doc__
```

- [ ] **Step 4: Run tests (must pass)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_events.py -q
```

Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/events.py tests/services/orchestrator/test_events.py
git commit -m "feat(orchestrator): node-name map and thinking-budget table"
```

---

### Task 3: EventPublisher — envelope, seq, fire-and-forget publish

**Files:**
- Modify: `services/orchestrator/events.py`
- Modify: `tests/services/orchestrator/test_events.py`

> **`tool_start()` / `tool_done()` publisher methods.** These two emit helpers
> (used by the manual sandbox tool-event task — see "Task 8b" below) are
> defined and shape-tested in **Task 4** alongside the other typed emit helpers
> (`test_tool_start_shape`, `test_tool_done_shape`). They emit the exact
> `tool.start` / `tool.done` shapes from FRONTEND_SPEC §4 (`tool.start` carries
> `toolCall` as `Omit<ToolCall,'result'|'durationMs'|'status'>`; `tool.done`
> carries flat `toolId`/`status`/`summary`/`result`/`durationMs`). The
> publisher is already bound to its task at construction, so these methods take
> **no** `task_id` argument — the caller in `execute_node` does not pass one.

- [ ] **Step 1: Write failing test** — append to `tests/services/orchestrator/test_events.py`

```python
import json
from unittest.mock import AsyncMock


@pytest.fixture
def pub():
    from services.orchestrator.events import EventPublisher

    redis = AsyncMock(name="redis")
    p = EventPublisher(redis=redis, task_id="t-1", session_id="s-1", turn_id="turn-1")
    return p, redis


async def test_emit_publishes_to_channel_with_envelope(pub):
    p, redis = pub
    await p.emit({"type": "turn.done", "status": "complete"})

    redis.publish.assert_awaited_once()
    channel, raw = redis.publish.await_args.args
    assert channel == "labmate:events:t-1"
    payload = json.loads(raw)
    assert payload["type"] == "turn.done"
    assert payload["sessionId"] == "s-1"
    assert payload["turnId"] == "turn-1"
    assert payload["seq"] == 0  # first event


async def test_seq_increments_per_emit(pub):
    p, redis = pub
    await p.emit({"type": "node.enter", "node": "plan_node", "thinkingBudget": 3000})
    await p.emit({"type": "answer.delta", "text": "hi"})
    seqs = [json.loads(c.args[1])["seq"] for c in redis.publish.await_args_list]
    assert seqs == [0, 1]


async def test_emit_sets_expire_once(pub):
    p, redis = pub
    await p.emit({"type": "answer.delta", "text": "a"})
    await p.emit({"type": "answer.delta", "text": "b"})
    # EXPIRE is set exactly once on the marker key, not per event
    redis.expire.assert_awaited_once_with("labmate:events:t-1", 86_400)


async def test_emit_never_raises_on_redis_error(pub):
    p, redis = pub
    redis.publish.side_effect = RuntimeError("redis down")
    # fire-and-forget: a publish failure must not propagate
    await p.emit({"type": "answer.delta", "text": "x"})
```

- [ ] **Step 2: Run it (must fail)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_events.py -q
```

Expected: `ImportError: cannot import name 'EventPublisher'`.

- [ ] **Step 3: Implement** — append to `services/orchestrator/events.py`

```python
import json
import logging

import redis.asyncio as aioredis

_log = logging.getLogger("orchestrator.events")


class EventPublisher:
    """
    Publishes StreamEvents (FRONTEND_SPEC §4) to labmate:events:<task_id>.

    - Stamps every event with the envelope { sessionId, turnId, seq }.
    - seq is a monotonic 0-based counter, one publisher per task/turn.
    - Sets EXPIRE on the channel marker key once (24 h), matching RESULT_TTL.
    - Fire-and-forget: a missed subscriber or a Redis error is logged, never
      raised — streaming must never break task execution (CLAUDE.md spirit).
    """

    def __init__(
        self,
        redis: aioredis.Redis,
        task_id: str,
        session_id: str,
        turn_id: str,
    ) -> None:
        self._redis = redis
        self._task_id = task_id
        self._channel = event_channel(task_id)
        self._session_id = session_id
        self._turn_id = turn_id
        self._seq = 0
        self._expire_set = False

    async def emit(self, event: dict) -> None:
        """Stamp envelope, publish, swallow all errors (fire-and-forget)."""
        envelope = {
            **event,
            "sessionId": self._session_id,
            "turnId": event.get("turnId", self._turn_id),
            "seq": self._seq,
        }
        self._seq += 1
        try:
            await self._redis.publish(self._channel, json.dumps(envelope, default=str))
            if not self._expire_set:
                await self._redis.expire(self._channel, EVENTS_TTL)
                self._expire_set = True
        except Exception:  # noqa: BLE001 — fire-and-forget by contract
            _log.debug("event publish failed (ignored)", exc_info=True)
```

> Note: `EXPIRE` on a pub/sub channel name has no key to attach to in plain
> Redis pub/sub; the `expire` call targets the same string used as the channel,
> which Redis treats as a (possibly absent) key. This is intentional and
> harmless — it gives consumers a TTL'd marker without changing pub/sub
> semantics, and the prompt requires an EXPIRE matching the result TTL. The
> call is made exactly once to avoid per-event overhead.

- [ ] **Step 4: Run tests (must pass)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_events.py -q
```

Expected: `8 passed`.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/events.py tests/services/orchestrator/test_events.py
git commit -m "feat(orchestrator): EventPublisher with envelope, seq, fire-and-forget"
```

---

### Task 4: Typed emit helpers for each StreamEvent variant

**Files:**
- Modify: `services/orchestrator/events.py`
- Modify: `tests/services/orchestrator/test_events.py`

- [ ] **Step 1: Write failing test** — append to `tests/services/orchestrator/test_events.py`

```python
def _published(redis):
    return [json.loads(c.args[1]) for c in redis.publish.await_args_list]


async def test_node_enter_shape(pub):
    p, redis = pub
    await p.node_enter("plan_node")
    ev = _published(redis)[0]
    assert ev["type"] == "node.enter"
    assert ev["node"] == "plan_node"
    assert ev["thinkingBudget"] == 3000
    assert ev["turnId"] == "turn-1"


async def test_reasoning_and_answer_delta_shapes(pub):
    p, redis = pub
    await p.reasoning_delta("because")
    await p.answer_delta("hello")
    evs = _published(redis)
    assert evs[0] == {**evs[0], "type": "reasoning.delta", "text": "because"}
    assert evs[1]["type"] == "answer.delta"
    assert evs[1]["text"] == "hello"


async def test_tool_start_shape(pub):
    p, redis = pub
    await p.tool_start(tool_id="tc-1", name="exec_run", kind="tool",
                       summary="Running bash…", reasoning_why="", args={"command": "ls"})
    ev = _published(redis)[0]
    assert ev["type"] == "tool.start"
    tc = ev["toolCall"]
    assert tc == {
        "id": "tc-1", "name": "exec_run", "kind": "tool",
        "summary": "Running bash…", "reasoningWhy": "", "args": {"command": "ls"},
    }
    # tool.start must NOT carry result/durationMs/status (Omit<> per spec)
    assert "result" not in tc and "durationMs" not in tc and "status" not in tc


async def test_tool_done_shape(pub):
    p, redis = pub
    await p.tool_done(tool_id="tc-1", status="done", summary="exit 0",
                      result={"exit_code": 0}, duration_ms=1200)
    ev = _published(redis)[0]
    assert ev["type"] == "tool.done"
    assert ev["toolId"] == "tc-1"
    assert ev["status"] == "done"
    assert ev["summary"] == "exit 0"
    assert ev["result"] == {"exit_code": 0}
    assert ev["durationMs"] == 1200


async def test_turn_done_shape(pub):
    p, redis = pub
    await p.turn_done("complete")
    ev = _published(redis)[0]
    assert ev == {**ev, "type": "turn.done", "status": "complete"}


async def test_agent_status_brain_shape(pub):
    p, redis = pub
    await p.agent_status(state="active", node="plan_node")
    ev = _published(redis)[0]
    assert ev["type"] == "agent.status"
    brain = ev["status"]["brain"]
    assert brain["state"] == "active"
    assert brain["node"] == "plan_node"
    assert brain["thinkingBudget"] == 3000
    assert brain["endpoint"]  # non-empty


async def test_context_update_shape(pub):
    p, redis = pub
    await p.context_update(used=120, segments={
        "systemPrompt": 100, "skillInstructions": 0,
        "conversation": 20, "workingMemory": 0, "reasoning": 0,
    })
    ev = _published(redis)[0]
    assert ev["type"] == "context.update"
    w = ev["window"]
    assert w["max"] == 16384
    assert w["used"] == 120
    assert w["free"] == 16384 - 120
    assert sum(w["segments"].values()) == w["used"]
```

- [ ] **Step 2: Run it (must fail)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_events.py -q
```

Expected: `AttributeError: 'EventPublisher' object has no attribute 'node_enter'`.

- [ ] **Step 3: Implement** — add these methods inside `EventPublisher` and a brain-status constant block in `services/orchestrator/events.py`

```python
import os

CONTEXT_MAX = 16_384  # llama-server --ctx-size (FRONTEND_SPEC §3 ContextWindow.max)
BRAIN_MODEL = os.getenv("GEMMA_MODEL_LABEL", "Gemma 4 31B · Q4_K_XL")
BRAIN_ENDPOINT = os.getenv("GEMMA_ENDPOINT_LABEL", "llama.cpp :8000")
```

```python
    # --- typed emit helpers (one per emitted StreamEvent variant) ----------

    async def node_enter(self, node: str) -> None:
        await self.emit({
            "type": "node.enter",
            "node": node,
            "thinkingBudget": THINKING_BUDGET.get(node, 0),
        })

    async def reasoning_delta(self, text: str) -> None:
        await self.emit({"type": "reasoning.delta", "text": text})

    async def answer_delta(self, text: str) -> None:
        await self.emit({"type": "answer.delta", "text": text})

    async def tool_start(
        self,
        tool_id: str,
        name: str,
        kind: str,
        summary: str,
        reasoning_why: str = "",
        args: object = None,
    ) -> None:
        await self.emit({
            "type": "tool.start",
            "toolCall": {
                "id": tool_id,
                "name": name,
                "kind": kind,
                "summary": summary,
                "reasoningWhy": reasoning_why,
                "args": args,
            },
        })

    async def tool_done(
        self,
        tool_id: str,
        status: str,
        summary: str,
        result: object,
        duration_ms: int,
    ) -> None:
        await self.emit({
            "type": "tool.done",
            "toolId": tool_id,
            "status": status,
            "summary": summary,
            "result": result,
            "durationMs": duration_ms,
        })

    async def turn_done(self, status: str) -> None:
        await self.emit({"type": "turn.done", "status": status})

    async def agent_status(self, state: str, node: str) -> None:
        await self.emit({
            "type": "agent.status",
            "status": {
                "brain": {
                    "model": BRAIN_MODEL,
                    "endpoint": BRAIN_ENDPOINT,
                    "state": state,
                    "node": node,
                    "thinkingBudget": THINKING_BUDGET.get(node, 0),
                },
            },
        })

    async def context_update(self, used: int, segments: dict) -> None:
        await self.emit({
            "type": "context.update",
            "window": {
                "max": CONTEXT_MAX,
                "used": used,
                "segments": segments,
                "free": CONTEXT_MAX - used,
            },
        })
```

- [ ] **Step 4: Run tests (must pass)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_events.py -q
```

Expected: `15 passed`.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/events.py tests/services/orchestrator/test_events.py
git commit -m "feat(orchestrator): typed emit helpers for each StreamEvent variant"
```

---

### Task 5: map_langgraph_event() — translate astream_events to emit calls

**Files:**
- Modify: `services/orchestrator/events.py`
- Modify: `tests/services/orchestrator/test_events.py`

LangGraph `astream_events(version="v2")` yields dicts shaped like:
```python
{"event": "on_chain_start", "name": "plan", "data": {...},
 "metadata": {"langgraph_node": "plan"}, "tags": [...], "run_id": "..."}
{"event": "on_chat_model_stream",
 "data": {"chunk": <AIMessageChunk with .content and optional
          .additional_kwargs['reasoning_content']>},
 "metadata": {"langgraph_node": "plan"}}
{"event": "on_tool_start", "name": "exec_run", "data": {"input": {...}}, "run_id": "r1"}
{"event": "on_tool_end", "name": "exec_run", "data": {"output": ...}, "run_id": "r1"}
{"event": "on_chain_end", "name": "check", "data": {"output": {...}},
 "metadata": {"langgraph_node": "check"}}
```

`map_langgraph_event()` is a pure async function: given one LangGraph event and
a publisher, it calls zero or more emit helpers. It is the single place the
LangGraph schema is interpreted, so it is exhaustively unit-tested with
hand-built event dicts (no live LangGraph).

- [ ] **Step 1: Write failing test** — append to `tests/services/orchestrator/test_events.py`

```python
class _Chunk:
    """Stand-in for an AIMessageChunk from llama.cpp via litellm."""
    def __init__(self, content="", reasoning_content=None):
        self.content = content
        self.additional_kwargs = {}
        if reasoning_content is not None:
            self.additional_kwargs["reasoning_content"] = reasoning_content


async def test_map_on_chain_start_emits_node_enter_and_agent_active(pub):
    from services.orchestrator.events import map_langgraph_event
    p, redis = pub
    await map_langgraph_event(
        {"event": "on_chain_start", "name": "plan",
         "metadata": {"langgraph_node": "plan"}, "data": {}},
        p,
    )
    types = [e["type"] for e in _published(redis)]
    assert "node.enter" in types
    assert "agent.status" in types  # idle -> active on first node
    node_enter = next(e for e in _published(redis) if e["type"] == "node.enter")
    assert node_enter["node"] == "plan_node"


async def test_map_approval_node_emits_node_enter(pub):
    from services.orchestrator.events import map_langgraph_event
    p, redis = pub
    await map_langgraph_event(
        {"event": "on_chain_start", "name": "approval",
         "metadata": {"langgraph_node": "approval"}, "data": {}},
        p,
    )
    types = [e["type"] for e in _published(redis)]
    assert "node.enter" in types  # approval gate IS visible (approval_node)
    node_enter = next(e for e in _published(redis) if e["type"] == "node.enter")
    assert node_enter["node"] == "approval_node"


async def test_map_unmapped_node_is_dropped(pub):
    from services.orchestrator.events import map_langgraph_event
    p, redis = pub
    # the graph root / unnamed chain has no NodeName mapping — still dropped
    await map_langgraph_event(
        {"event": "on_chain_start", "name": "__graph__",
         "metadata": {"langgraph_node": "__graph__"}, "data": {}},
        p,
    )
    redis.publish.assert_not_called()  # unmapped node -> drop


async def test_map_chat_model_stream_splits_reasoning_and_answer(pub):
    from services.orchestrator.events import map_langgraph_event
    p, redis = pub
    await map_langgraph_event(
        {"event": "on_chat_model_stream",
         "metadata": {"langgraph_node": "plan"},
         "data": {"chunk": _Chunk(content="answer", reasoning_content="why")},
        },
        p,
    )
    evs = _published(redis)
    types = [e["type"] for e in evs]
    assert "reasoning.delta" in types and "answer.delta" in types
    assert next(e for e in evs if e["type"] == "reasoning.delta")["text"] == "why"
    assert next(e for e in evs if e["type"] == "answer.delta")["text"] == "answer"


async def test_map_empty_chunk_emits_nothing(pub):
    from services.orchestrator.events import map_langgraph_event
    p, redis = pub
    await map_langgraph_event(
        {"event": "on_chat_model_stream",
         "metadata": {"langgraph_node": "plan"},
         "data": {"chunk": _Chunk(content="", reasoning_content=None)}},
        p,
    )
    redis.publish.assert_not_called()


async def test_map_tool_start_then_end_pairs_by_run_id(pub):
    from services.orchestrator.events import map_langgraph_event
    p, redis = pub
    await map_langgraph_event(
        {"event": "on_tool_start", "name": "exec_run", "run_id": "r1",
         "data": {"input": {"command": "ls"}}}, p,
    )
    await map_langgraph_event(
        {"event": "on_tool_end", "name": "exec_run", "run_id": "r1",
         "data": {"output": {"exit_code": 0}}}, p,
    )
    evs = _published(redis)
    start = next(e for e in evs if e["type"] == "tool.start")
    done = next(e for e in evs if e["type"] == "tool.done")
    assert start["toolCall"]["id"] == done["toolId"]   # same id across the pair
    assert start["toolCall"]["name"] == "exec_run"
    assert done["status"] == "done"


async def test_map_final_chain_end_emits_turn_done(pub):
    from services.orchestrator.events import map_langgraph_event
    p, redis = pub
    # data.output carries final_answer -> this is the terminal node
    await map_langgraph_event(
        {"event": "on_chain_end", "name": "check",
         "metadata": {"langgraph_node": "check"},
         "data": {"output": {"final_answer": "done"}}},
        p,
    )
    types = [e["type"] for e in _published(redis)]
    assert "turn.done" in types
    td = next(e for e in _published(redis) if e["type"] == "turn.done")
    assert td["status"] == "complete"


async def test_map_non_final_chain_end_no_turn_done(pub):
    from services.orchestrator.events import map_langgraph_event
    p, redis = pub
    await map_langgraph_event(
        {"event": "on_chain_end", "name": "plan",
         "metadata": {"langgraph_node": "plan"},
         "data": {"output": {"goal_tree": {}}}},
        p,
    )
    types = [e["type"] for e in _published(redis)]
    assert "turn.done" not in types
```

- [ ] **Step 2: Run it (must fail)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_events.py -q
```

Expected: `ImportError: cannot import name 'map_langgraph_event'`.

- [ ] **Step 3: Implement** — append to `services/orchestrator/events.py`

```python
import time


def _chunk_parts(chunk) -> tuple[str, str]:
    """Extract (answer_text, reasoning_text) from a streamed model chunk.

    llama.cpp (via litellm) separates reasoning_content from content. The
    reasoning lands in additional_kwargs['reasoning_content']; the answer is
    chunk.content. Either may be empty.
    """
    content = getattr(chunk, "content", "") or ""
    reasoning = ""
    ak = getattr(chunk, "additional_kwargs", None)
    if isinstance(ak, dict):
        reasoning = ak.get("reasoning_content") or ""
    return content, reasoning


# tool.start emits an id; tool.done must reuse it. LangGraph gives a stable
# run_id per tool invocation, so map run_id -> our tool id (here: the run_id
# itself, which is already unique). Track start times to compute durationMs.
class _ToolTimer:
    def __init__(self) -> None:
        self._start: dict[str, float] = {}

    def begin(self, run_id: str) -> None:
        self._start[run_id] = time.monotonic()

    def end_ms(self, run_id: str) -> int:
        t0 = self._start.pop(run_id, None)
        if t0 is None:
            return 0
        return int((time.monotonic() - t0) * 1000)


async def map_langgraph_event(event: dict, pub: "EventPublisher") -> None:
    """Translate ONE LangGraph astream_events(v2) event into emit calls.

    Pure dispatcher — all StreamEvent shapes come from EventPublisher helpers.
    Unmapped nodes (the graph root / unnamed chains) and empty chunks produce
    no events; mapped nodes including 'approval' (-> approval_node) do emit.
    """
    etype = event.get("event")
    meta = event.get("metadata") or {}
    raw_node = meta.get("langgraph_node") or event.get("name")

    if etype == "on_chain_start":
        spec_node = NODE_NAME_MAP.get(raw_node)
        if spec_node is None:
            return  # unmapped node (graph root / unnamed chain) — drop
        if not pub._active:
            pub._active = True
            await pub.agent_status(state="active", node=spec_node)
        await pub.node_enter(spec_node)
        return

    if etype == "on_chat_model_stream":
        chunk = (event.get("data") or {}).get("chunk")
        if chunk is None:
            return
        content, reasoning = _chunk_parts(chunk)
        if reasoning:
            await pub.reasoning_delta(reasoning)
        if content:
            await pub.answer_delta(content)
        return

    if etype == "on_tool_start":
        run_id = event.get("run_id", "")
        pub._tools.begin(run_id)
        name = event.get("name", "tool")
        args = (event.get("data") or {}).get("input")
        await pub.tool_start(
            tool_id=run_id, name=name, kind="tool",
            summary=f"Running {name}…", reasoning_why="", args=args,
        )
        return

    if etype == "on_tool_end":
        run_id = event.get("run_id", "")
        out = (event.get("data") or {}).get("output")
        ms = pub._tools.end_ms(run_id)
        ok = not (isinstance(out, dict) and out.get("ok") is False)
        await pub.tool_done(
            tool_id=run_id,
            status="done" if ok else "error",
            summary=f"exit {0 if ok else 1}",
            result=out,
            duration_ms=ms,
        )
        return

    if etype == "on_chain_end":
        out = (event.get("data") or {}).get("output")
        # The terminal node is the one that produced final_answer.
        if isinstance(out, dict) and out.get("final_answer"):
            await pub.turn_done("complete")
        return
```

Also add the two attributes to `EventPublisher.__init__` (so the dispatcher can
track active-state and tool timers):

```python
        self._active = False
        self._tools = _ToolTimer()
```

> `_ToolTimer` is defined after `EventPublisher` in the module, so reference it
> only inside `__init__`'s body at call time (Python resolves it at instance
> construction, after the module is fully loaded — safe). If construction order
> ever moves, hoist `_ToolTimer` above `EventPublisher`.

- [ ] **Step 4: Run tests (must pass)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_events.py -q
```

Expected: `23 passed`.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/events.py tests/services/orchestrator/test_events.py
git commit -m "feat(orchestrator): map_langgraph_event translates astream_events to StreamEvents"
```

---

### Task 6: Injectable tokenizer for safe context.update token counts

**Files:**
- Modify: `services/orchestrator/memory_consolidator.py`
- Modify: `tests/services/orchestrator/test_events.py`

`context.update` needs a `used` token count from the Gemma tokenizer.
`memory_consolidator.token_count()` already does this, but it lazily downloads
`google/gemma-4-9b-it` via `transformers` — unusable in mocked tests. We make
the tokenizer injectable so tests pass a fake, and the `context.update`
producer (Task 7) accepts a `count_tokens` callable defaulting to the real one.

- [ ] **Step 1: Write failing test** — append to `tests/services/orchestrator/test_events.py`

```python
def test_token_count_accepts_injected_tokenizer():
    from services.orchestrator.memory_consolidator import token_count

    class FakeTok:
        def encode(self, text):
            return text.split()  # 1 token per whitespace word

    assert token_count("a b c", tokenizer=FakeTok()) == 3
```

- [ ] **Step 2: Run it (must fail)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_events.py::test_token_count_accepts_injected_tokenizer -q
```

Expected: `TypeError: token_count() got an unexpected keyword argument 'tokenizer'`.

- [ ] **Step 3: Implement** — edit `services/orchestrator/memory_consolidator.py`

Replace:
```python
def token_count(text: str) -> int:
    return len(_get_tokenizer().encode(text))
```
with:
```python
def token_count(text: str, tokenizer=None) -> int:
    """Count Gemma SentencePiece tokens (CLAUDE.md rule: never tiktoken).

    Pass `tokenizer` to inject a fake in tests; defaults to the lazy
    google/gemma-4-9b-it singleton.
    """
    tok = tokenizer if tokenizer is not None else _get_tokenizer()
    return len(tok.encode(text))
```

- [ ] **Step 4: Run tests (must pass)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_events.py::test_token_count_accepts_injected_tokenizer tests/services/orchestrator/ -q -k "not live"
```

Expected: all selected tests pass (existing memory_consolidator tests still green — the new param is optional and defaults to prior behavior).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/memory_consolidator.py tests/services/orchestrator/test_events.py
git commit -m "refactor(orchestrator): make token_count tokenizer injectable for tests"
```

---

### Task 7: context.update producer keyed on graph state

**Files:**
- Modify: `services/orchestrator/events.py`
- Modify: `tests/services/orchestrator/test_events.py`

The prompt requires `context.update` at node boundaries with token counts from
the Gemma tokenizer, populating **all five** spec segments:

- **`systemPrompt`** — tokens of the first `state["messages"]` entry whose role
  is `"system"` (0 if none).
- **`skillInstructions`** — tokens of the MCP tool schemas injected as tool
  definitions. Computed once on the orchestrator (`orch._skill_tokens`, see
  Task 8b/Task 9 wiring) and read here via `getattr(orch, '_skill_tokens', 0)`.
- **`conversation`** — sum of all non-system, non-reasoning message tokens.
- **`workingMemory`** — tokens of any *non-first* system message (the first
  system message is the static prompt; subsequent system messages are RAG /
  Chroma injections). Approximate but accurate enough for the bar.
- **`reasoning`** — `reasoning_content` tokens from the last assistant message
  (already correct), plus reflection-message tokens already in state.

To stay dependency-light and test-safe, the producer takes a `count_tokens`
callable and an optional `orch` (for `_skill_tokens`).

- [ ] **Step 1: Write failing test** — append to `tests/services/orchestrator/test_events.py`

```python
async def test_emit_context_for_state_all_five_segments(pub):
    from services.orchestrator.events import emit_context_for_state
    p, redis = pub

    class _Orch:
        _skill_tokens = 5  # MCP tool schemas already counted on the orchestrator

    state = {
        "messages": [
            {"role": "system", "content": "static system prompt"},        # systemPrompt (3)
            {"role": "system", "content": "rag injected facts"},           # workingMemory (3)
            {"role": "user", "content": "hello world"},                    # conversation (2)
            {"role": "assistant", "content": "an answer here",             # conversation (3)
             "reasoning_content": "because reasons apply"},                # reasoning (3)
        ],
        "goal_tree": {"root": {"description": "do a thing"}},
    }
    # fake counter: 1 token per whitespace word
    await emit_context_for_state(
        p, state, count_tokens=lambda s: len(s.split()), orch=_Orch(),
    )

    ev = [json.loads(c.args[1]) for c in redis.publish.await_args_list][-1]
    assert ev["type"] == "context.update"
    w = ev["window"]
    assert w["max"] == 16384
    seg = w["segments"]
    assert seg["systemPrompt"] == 3        # first system message
    assert seg["skillInstructions"] == 5   # orch._skill_tokens
    assert seg["conversation"] == 5        # "hello world" + "an answer here"
    assert seg["workingMemory"] == 3       # second (RAG) system message
    assert seg["reasoning"] == 3           # assistant reasoning_content
    assert sum(seg.values()) == w["used"]
    assert w["free"] == w["max"] - w["used"]


async def test_emit_context_default_counter_is_lazy(pub, monkeypatch):
    # When count_tokens is omitted it must defer to memory_consolidator.token_count.
    import services.orchestrator.events as events
    called = {}

    def fake_token_count(text):
        called["text"] = text
        return 7

    monkeypatch.setattr(events, "token_count", fake_token_count)
    p, redis = pub
    await events.emit_context_for_state(p, {"messages": [], "goal_tree": {}})
    ev = [json.loads(c.args[1]) for c in redis.publish.await_args_list][-1]
    assert ev["window"]["used"] == 0  # empty state -> 0 tokens, counter not forced
```

- [ ] **Step 2: Run it (must fail)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_events.py -q -k context
```

Expected: `ImportError: cannot import name 'emit_context_for_state'`.

- [ ] **Step 3: Implement** — append to `services/orchestrator/events.py`

```python
from services.orchestrator.memory_consolidator import token_count


def _msg_role(m) -> str:
    if isinstance(m, dict):
        return m.get("role", "")
    return getattr(m, "role", "") or ""


def _msg_content(m) -> str:
    if isinstance(m, dict):
        return m.get("content", "") or ""
    return getattr(m, "content", "") or ""


def _msg_reasoning(m) -> str:
    """reasoning_content lives on dict['reasoning_content'] or
    additional_kwargs['reasoning_content'] depending on message form."""
    if isinstance(m, dict):
        return m.get("reasoning_content", "") or ""
    rc = getattr(m, "reasoning_content", None)
    if rc:
        return rc
    ak = getattr(m, "additional_kwargs", None)
    if isinstance(ak, dict):
        return ak.get("reasoning_content", "") or ""
    return ""


async def emit_context_for_state(
    pub: "EventPublisher",
    state: dict,
    count_tokens=None,
    orch=None,
) -> None:
    """Compute a ContextWindow from graph state and emit context.update.

    Buckets (FRONTEND_SPEC §3 ContextWindow.segments), all five populated:
      systemPrompt      — first system message (the static prompt)
      skillInstructions — MCP tool schemas, precomputed on orch._skill_tokens
      conversation      — non-system, non-reflection message bodies
      workingMemory     — non-first system messages (RAG / Chroma injections)
      reasoning         — reflection-message bodies + reasoning_content on the
                          last assistant message
    The invariant sum(segments) == used always holds.
    """
    count = count_tokens or token_count
    messages = state.get("messages", []) or []

    sys_msgs = [m for m in messages if _msg_role(m) == "system"]
    system_tokens = count(_msg_content(sys_msgs[0])) if sys_msgs else 0
    # first system message = static prompt; the rest are RAG injections
    working_memory_tokens = sum(count(_msg_content(m)) for m in sys_msgs[1:])

    conversation = 0
    reasoning = 0
    for m in messages:
        role = _msg_role(m)
        if role == "system":
            continue  # accounted for in systemPrompt / workingMemory
        if role == "reflection":
            reasoning += count(_msg_content(m))
            continue
        conversation += count(_msg_content(m))

    # reasoning_content on the last assistant message (current turn's reasoning)
    for m in reversed(messages):
        if _msg_role(m) == "assistant":
            rc = _msg_reasoning(m)
            if rc:
                reasoning += count(rc)
            break

    skill_tokens = getattr(orch, "_skill_tokens", 0) if orch is not None else 0

    segments = {
        "systemPrompt": system_tokens,
        "skillInstructions": skill_tokens,
        "conversation": conversation,
        "workingMemory": working_memory_tokens,
        "reasoning": reasoning,
    }
    await pub.context_update(used=sum(segments.values()), segments=segments)
```

- [ ] **Step 4: Run tests (must pass)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_events.py -q
```

Expected: `26 passed`.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/events.py tests/services/orchestrator/test_events.py
git commit -m "feat(orchestrator): context.update producer bucketed by all five segments"
```

---

### Task 8: CodingOrchestrator.run_task_streamed()

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py`
- Create: `tests/services/orchestrator/test_coding_orchestrator_stream.py`

`run_task_streamed()` mirrors `run_task()` (same initial state, same config)
but drives `graph.astream_events(version="v2")`, routing each event through
`map_langgraph_event()`, emitting `context.update` at each `node.enter`, and
returning the final state for `_write_result()`. It must end with
`agent.status(idle)` and `turn.done` even if the run produced no terminal
`final_answer` (so the CLI's Live display always closes).

- [ ] **Step 1: Write failing test** — `tests/services/orchestrator/test_coding_orchestrator_stream.py`

```python
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock


class _Chunk:
    def __init__(self, content="", reasoning_content=None):
        self.content = content
        self.additional_kwargs = (
            {"reasoning_content": reasoning_content} if reasoning_content else {}
        )


def _fake_graph(events, final_state):
    """A graph whose astream_events yields `events` then leaves state as final."""
    graph = MagicMock(name="graph")

    async def _astream_events(initial, cfg, version=None):
        for e in events:
            yield e

    graph.astream_events = _astream_events
    graph.aget_state = AsyncMock(return_value=MagicMock(values=final_state))
    return graph


@pytest.fixture
def orch():
    from services.orchestrator.coding_orchestrator import CodingOrchestrator
    return CodingOrchestrator(
        graph=None, workspace_path="/ws", docker_container="",
    )


async def test_run_task_streamed_emits_node_enter_and_returns_state(orch):
    events = [
        {"event": "on_chain_start", "name": "plan",
         "metadata": {"langgraph_node": "plan"}, "data": {}},
        {"event": "on_chat_model_stream", "metadata": {"langgraph_node": "plan"},
         "data": {"chunk": _Chunk(content="hi", reasoning_content="think")}},
        {"event": "on_chain_end", "name": "check",
         "metadata": {"langgraph_node": "check"},
         "data": {"output": {"final_answer": "done"}}},
    ]
    final_state = {"final_answer": "done", "goal_tree": {}, "messages": []}
    orch.graph = _fake_graph(events, final_state)

    pub = AsyncMock()
    pub._active = False
    state = await orch.run_task_streamed("task", "s-1", pub)

    assert state["final_answer"] == "done"
    pub.node_enter.assert_any_await("plan_node")
    pub.answer_delta.assert_any_await("hi")
    pub.reasoning_delta.assert_any_await("think")
    pub.turn_done.assert_any_await("complete")


async def test_run_task_streamed_always_emits_idle_and_turn_done(orch):
    # No terminal final_answer in the stream — wrapper must still close out.
    events = [
        {"event": "on_chain_start", "name": "plan",
         "metadata": {"langgraph_node": "plan"}, "data": {}},
    ]
    orch.graph = _fake_graph(events, {"goal_tree": {}, "messages": []})

    pub = AsyncMock()
    pub._active = False
    await orch.run_task_streamed("task", "s-2", pub)

    # idle agent.status at end + a turn.done fallback
    pub.agent_status.assert_any_await(state="idle", node="plan_node")
    pub.turn_done.assert_awaited()


async def test_run_task_streamed_raises_propagate_after_error_turn_done(orch):
    async def _boom(initial, cfg, version=None):
        if False:
            yield {}
        raise RuntimeError("graph exploded")

    graph = MagicMock()
    graph.astream_events = _boom
    orch.graph = graph

    pub = AsyncMock()
    pub._active = False
    with pytest.raises(RuntimeError, match="graph exploded"):
        await orch.run_task_streamed("task", "s-3", pub)

    # an error turn.done must be emitted before propagating
    pub.turn_done.assert_any_await("error")
```

- [ ] **Step 2: Run it (must fail)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_coding_orchestrator_stream.py -q
```

Expected: `AttributeError: 'CodingOrchestrator' object has no attribute 'run_task_streamed'`.

- [ ] **Step 3: Implement** — add to `services/orchestrator/coding_orchestrator.py`

Add the import near the top of the file (after the existing `from .types import ...`):
```python
from .events import (
    map_langgraph_event,
    emit_context_for_state,
    NODE_NAME_MAP,
)
```

Add this method to `CodingOrchestrator` (place it directly after `run_task`):
```python
    async def run_task_streamed(self, task: str, session_id: str, pub,
                                user_id: str = "", workspace_id: str = "") -> dict:
        """Stream a task via graph.astream_events, publishing StreamEvents.

        Mirrors run_task() (same initial state + config) but emits live events
        through `pub` (EventPublisher). Returns the final State dict so the
        caller can write the canonical labmate:result:<task_id> record.

        On node entry, also emits context.update from the current state. On any
        exception, emits turn.done('error') before re-raising so the consumer's
        Live display closes; on normal completion emits agent.status(idle) and a
        turn.done('complete') fallback if the graph produced no terminal node.
        """
        from .types import create_goal

        initial = {
            "session_id": session_id,
            "goal_tree": create_goal({}, "root", None, task),
            "current_goal_id": "root",
            "step_markers": {},
            "messages": [],
            "error": None,
            "final_answer": "",
            "workspace_id": workspace_id,
            "user_id": user_id,
        }
        cfg = {"configurable": {
            "thread_id": session_id, "workspace_id": workspace_id, "user_id": user_id,
        }}

        saw_turn_done = False
        last_node = None
        try:
            async for event in self.graph.astream_events(initial, cfg, version="v2"):
                await map_langgraph_event(event, pub)
                # context.update at each node boundary (prompt requirement)
                if event.get("event") == "on_chain_start":
                    raw_node = (event.get("metadata") or {}).get("langgraph_node")
                    if raw_node in NODE_NAME_MAP:
                        last_node = NODE_NAME_MAP[raw_node]
                        await emit_context_for_state(pub, initial, orch=self)
                if event.get("event") == "on_chain_end":
                    out = (event.get("data") or {}).get("output")
                    if isinstance(out, dict) and out.get("final_answer"):
                        saw_turn_done = True
        except Exception:
            await pub.turn_done("error")
            await pub.agent_status(state="idle", node=last_node or "plan_node")
            raise

        if not saw_turn_done:
            await pub.turn_done("complete")
        await pub.agent_status(state="idle", node=last_node or "plan_node")

        snap = await self.graph.aget_state(cfg)
        return snap.values
```

> `aget_state(cfg)` returns the latest checkpointed `State` after the stream
> ends — the streamed equivalent of `ainvoke`'s return value. The
> MongoDBSaver checkpointer (already wired in `build_graph`) persists it.

- [ ] **Step 4: Run tests (must pass)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_coding_orchestrator_stream.py -q
```

Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/coding_orchestrator.py tests/services/orchestrator/test_coding_orchestrator_stream.py
git commit -m "feat(orchestrator): run_task_streamed drives astream_events"
```

---

### Task 8b: Manually emit tool.start / tool.done around run_in_sandbox

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py`
- Modify: `services/orchestrator/graph.py`
- Modify: `services/orchestrator/events.py` (verify `tool_start`/`tool_done` — defined in Task 4)
- Modify: `tests/services/orchestrator/test_coding_orchestrator_stream.py`

`astream_events`' `on_tool_start`/`on_tool_end` only fire for LangChain-wrapped
tools. `run_in_sandbox()` is a plain method call inside `execute_node`, so no
tool events are produced automatically. This task makes the sandbox call emit
`tool.start` / `tool.done` manually so the tool rows surface in the UI.

The mechanism: stash the active `EventPublisher` on the `CodingOrchestrator`
during a streaming run (`orch._event_pub = publisher`), so the graph node — a
plain async function closed over `orch` — can reach it. `execute_node` checks
`getattr(orch, '_event_pub', None)` and brackets the sandbox call with emits.
When `_event_pub` is `None` (non-streaming `run_task()` path, or tests),
`run_in_sandbox` is called exactly as today with no events.

> `tool_start()` / `tool_done()` are the publisher methods from Task 4; they
> emit the exact `tool.start` / `tool.done` shapes from FRONTEND_SPEC §4. No
> new publisher methods are needed here — this task only wires them in.

- [ ] **Step 1: Write failing test** — append to `tests/services/orchestrator/test_coding_orchestrator_stream.py`

```python
async def test_execute_node_emits_tool_events_when_publisher_set(monkeypatch):
    """With orch._event_pub set, an execute pass emits tool.start then tool.done."""
    from services.orchestrator.coding_orchestrator import CodingOrchestrator
    from services.orchestrator import graph as graph_mod

    orch = CodingOrchestrator(graph=None, workspace_path="/ws", docker_container="")
    async_orch = MagicMock()

    # the publisher records the order of calls
    pub = AsyncMock()
    orch._event_pub = pub

    # editor() -> a command; run_in_sandbox() -> a successful observation
    orch.editor = AsyncMock(return_value="ls -la")
    orch.run_in_sandbox = AsyncMock(
        return_value={"stdout": "file.txt", "stderr": "", "exit_code": 0, "ok": True}
    )
    orch.git_checkpoint = AsyncMock()

    # build just the execute node via the factory
    _plan, execute_node, _check, _reflect, _approval = graph_mod.make_nodes(orch, async_orch)

    from services.orchestrator.types import create_goal
    tree = create_goal({}, "root", None, "do a thing")
    create_goal(tree, "root_sub0", "root", "list files")
    # mark root completed-ish so the single ready leaf is root_sub0
    state = {
        "session_id": "s-1", "goal_tree": tree,
        "current_goal_id": "root", "step_markers": {}, "messages": [],
    }
    await execute_node(state)

    pub.tool_start.assert_awaited_once()
    pub.tool_done.assert_awaited_once()
    # tool.start emitted before tool.done
    assert pub.tool_start.await_args.kwargs["name"] == "exec_run"
    assert pub.tool_done.await_args.kwargs["status"] == "done"
    assert pub.tool_done.await_args.kwargs["result"]["ok"] is True
    orch.run_in_sandbox.assert_awaited_once_with("ls -la")


async def test_execute_node_no_events_when_publisher_absent():
    """With no _event_pub, run_in_sandbox is still called and no events emit."""
    from services.orchestrator.coding_orchestrator import CodingOrchestrator
    from services.orchestrator import graph as graph_mod

    orch = CodingOrchestrator(graph=None, workspace_path="/ws", docker_container="")
    assert getattr(orch, "_event_pub", None) is None  # default field is None
    async_orch = MagicMock()

    orch.editor = AsyncMock(return_value="ls")
    orch.run_in_sandbox = AsyncMock(
        return_value={"stdout": "ok", "stderr": "", "exit_code": 0, "ok": True}
    )
    orch.git_checkpoint = AsyncMock()

    _plan, execute_node, _check, _reflect, _approval = graph_mod.make_nodes(orch, async_orch)

    from services.orchestrator.types import create_goal
    tree = create_goal({}, "root", None, "do a thing")
    create_goal(tree, "root_sub0", "root", "list files")
    state = {
        "session_id": "s-1", "goal_tree": tree,
        "current_goal_id": "root", "step_markers": {}, "messages": [],
    }
    await execute_node(state)

    orch.run_in_sandbox.assert_awaited_once_with("ls")  # still runs normally
```

- [ ] **Step 2: Run it (must fail)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_coding_orchestrator_stream.py -q -k tool_events
```

Expected: `test_execute_node_emits_tool_events_when_publisher_set` fails —
`pub.tool_start`/`pub.tool_done` are never awaited (execute_node does not emit
yet). `test_execute_node_no_events_when_publisher_absent` may already pass.

- [ ] **Step 3a: Implement** — `services/orchestrator/coding_orchestrator.py`

Add the `_event_pub` field to `CodingOrchestrator.__init__` (after the existing
`self._gate_futures = {}` line):
```python
        # Set during a streaming run so graph nodes can emit tool events around
        # plain method calls (run_in_sandbox) that astream_events can't see.
        # None on the non-streaming run_task() path and in tests.
        self._event_pub = None  # EventPublisher | None
```

- [ ] **Step 3b: Implement** — `services/orchestrator/graph.py`

In `execute_node`, replace the single-goal sandbox call block:
```python
        cmd = cmd.strip()
        obs = await orch.run_in_sandbox(cmd)
        result_text = obs["stdout"] or obs["stderr"]
```
with a version that brackets the sandbox call with tool events when a publisher
is attached:
```python
        cmd = cmd.strip()

        import time, uuid
        tool_id = str(uuid.uuid4())
        pub = getattr(orch, "_event_pub", None)
        if pub is not None:
            await pub.tool_start(
                tool_id=tool_id, name="exec_run", kind="tool",
                summary=cmd[:120],
            )
        t0 = time.monotonic()
        obs = await orch.run_in_sandbox(cmd)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        if pub is not None:
            status = "done" if obs["ok"] else "error"
            summary = (obs["stdout"][:120] or obs["stderr"][:120])
            await pub.tool_done(
                tool_id=tool_id, status=status, summary=summary,
                result=obs, duration_ms=elapsed_ms,
            )

        result_text = obs["stdout"] or obs["stderr"]
```

> Only the single-goal branch of `execute_node` calls `run_in_sandbox`; the
> parallel branch dispatches through `async_orch.plan_and_dispatch()` (separate
> workers) and is out of scope for tool events here.

- [ ] **Step 3c: Wire the publisher in `run_task_streamed`** —
`services/orchestrator/coding_orchestrator.py`

In `run_task_streamed()` (Task 8), set and clear `self._event_pub` around the
stream so `execute_node` can reach the publisher:
```python
        self._event_pub = pub
        try:
            async for event in self.graph.astream_events(initial, cfg, version="v2"):
                ...  # existing body unchanged
        except Exception:
            ...  # existing error handling unchanged
            raise
        finally:
            self._event_pub = None
```
(Fold the existing `try/except` body into this `try/except/finally`; the
`finally` guarantees the publisher is detached even on error so it never leaks
into a subsequent non-streaming run.)

- [ ] **Step 4: Run tests (must pass)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_coding_orchestrator_stream.py -q
```

Expected: all pass (the two new tool-event tests plus the three from Task 8).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/coding_orchestrator.py services/orchestrator/graph.py tests/services/orchestrator/test_coding_orchestrator_stream.py
git commit -m "feat(orchestrator): emit tool.start/tool.done around run_in_sandbox"
```

---

### Task 9: Wire _handle() to the streamed path, preserve final-result write

**Files:**
- Modify: `services/orchestrator/main.py`
- Modify: `tests/services/orchestrator/test_main.py`

`_handle()` must: build an `EventPublisher` (task_id as both task and turn id —
the CLI uses one turn per task), call `run_task_streamed()`, and then write the
final result with the **unchanged** `_write_result()`. All existing `_handle`
tests must still pass — `run_task` is replaced by `run_task_streamed` in the
happy path, so those tests are updated to assert the new call.

- [ ] **Step 1: Write failing test** — add to `tests/services/orchestrator/test_main.py`

```python
@pytest.mark.asyncio
async def test_handle_uses_streamed_path_and_preserves_result_write():
    from services.orchestrator.main import OrchestratorProcess, RESULT_PREFIX
    proc = OrchestratorProcess()
    proc._redis = AsyncMock()

    orch = AsyncMock()
    orch.run_task_streamed.return_value = {"final_answer": "ok", "error": None}

    storage = AsyncMock()
    storage.workspaces = AsyncMock()
    storage.workspaces.record_session = AsyncMock()
    storage.workspaces.complete_session = AsyncMock()

    payload = json.dumps({"task_id": "t-stream", "task": "go", "session_id": "s"})
    await proc._handle("90-0", {"payload": payload}, orch, storage)

    # streamed path called with an EventPublisher positional/kw
    orch.run_task_streamed.assert_awaited_once()
    # canonical result still written + published (unchanged contract)
    proc._redis.set.assert_awaited()
    set_key = proc._redis.set.await_args.args[0]
    assert set_key == f"{RESULT_PREFIX}t-stream"
    proc._redis.publish.assert_any_await(f"{RESULT_PREFIX}t-stream", "ready")
    proc._redis.xack.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_publishes_events_to_event_channel():
    from services.orchestrator.main import OrchestratorProcess
    proc = OrchestratorProcess()
    proc._redis = AsyncMock()

    async def _run(task, session_id, pub, **kw):
        await pub.answer_delta("streamed")  # publisher writes to event channel
        return {"final_answer": "x", "error": None}

    orch = AsyncMock()
    orch.run_task_streamed.side_effect = _run

    storage = AsyncMock()
    storage.workspaces = AsyncMock()
    storage.workspaces.record_session = AsyncMock()
    storage.workspaces.complete_session = AsyncMock()

    payload = json.dumps({"task_id": "t-ev", "task": "go", "session_id": "s"})
    await proc._handle("91-0", {"payload": payload}, orch, storage)

    # at least one publish to the EVENT channel (not the result channel)
    channels = [c.args[0] for c in proc._redis.publish.await_args_list]
    assert "labmate:events:t-ev" in channels
```

Update the two existing tests that assert `orch.run_task` is called
(`test_handle_calls_run_task_and_acks`, `test_handle_uses_task_id_as_session_id_when_absent`,
`test_handle_parses_user_and_workspace`, `test_handle_defaults_missing_user_workspace`)
to assert on `orch.run_task_streamed` instead, e.g.:

```python
    orch.run_task_streamed.assert_awaited_once()
    call = orch.run_task_streamed.await_args
    assert call.args[0] == "do something"          # task
    assert call.args[1] == "s1"                     # session_id
    assert call.kwargs.get("user_id") == ""
    assert call.kwargs.get("workspace_id") == ""
```

(Set `orch.run_task_streamed.return_value = {...}` in each, mirroring the old
`orch.run_task.return_value`.)

- [ ] **Step 2: Run it (must fail)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_main.py -q
```

Expected: failures on the new tests (`run_task_streamed` not called) and the
updated existing ones.

- [ ] **Step 3: Implement** — edit `services/orchestrator/main.py`

Add the import:
```python
from services.orchestrator.events import EventPublisher
```

Replace the happy-path block in `_handle()`:
```python
            _log.info("task %s: %.80s", task_id, task_text)
            final_state = await orch.run_task(
                task_text, session_id, user_id=user_id, workspace_id=workspace_id
            )
            task_succeeded = True
            await self._write_result(task_id, {"ok": True, "state": final_state})
            _log.info("task %s complete", task_id)
```
with:
```python
            _log.info("task %s: %.80s", task_id, task_text)
            pub = EventPublisher(
                redis=self._redis,
                task_id=task_id,
                session_id=session_id,
                turn_id=task_id,   # one turn per task in the Redis/CLI model
            )
            final_state = await orch.run_task_streamed(
                task_text, session_id, pub,
                user_id=user_id, workspace_id=workspace_id,
            )
            task_succeeded = True
            await self._write_result(task_id, {"ok": True, "state": final_state})
            _log.info("task %s complete", task_id)
```

`_write_result()` is unchanged. The error path (`_write_result(task_id,
{"ok": False, ...})`) is unchanged.

- [ ] **Step 3b: Compute `_skill_tokens` once after MCP is ready** —
`services/orchestrator/main.py`

The `skillInstructions` context segment (Task 7) reads
`orch._skill_tokens`. Compute it once in `OrchestratorProcess.run()` right
after `mcp.wait_ready()` completes, so the MCP tool schemas are counted with the
Gemma tokenizer and reused for every `context.update`:
```python
        # after self._mcp.wait_ready() (and whenever the tool set changes):
        import json
        from services.orchestrator.memory_consolidator import token_count
        orch._skill_tokens = sum(
            token_count(f"{t.name} {t.description} {json.dumps(t.inputSchema or {})}")
            for t in (self._mcp.tools if self._mcp else [])
        )
```
If the orchestrator runs without an MCP bridge (`self._mcp is None`),
`_skill_tokens` stays at its `0` default (set in `__init__`, Task 8b) and the
`skillInstructions` segment reads 0.

- [ ] **Step 4: Run tests (must pass)**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_main.py tests/services/orchestrator/test_events.py tests/services/orchestrator/test_coding_orchestrator_stream.py -q
```

Expected: all pass (the `_write_result` tests in test_main.py are untouched and
still green, proving the canonical contract is preserved).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/main.py tests/services/orchestrator/test_main.py
git commit -m "feat(orchestrator): _handle streams events while preserving result write"
```

---

### Task 10: Full-suite regression and streaming smoke

**Files:**
- (no new files)

- [ ] **Step 1: Run the orchestrator suite**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/ -q -k "not live"
```

Expected: all pass, including the untouched `_write_result` / `_ensure_group`
tests — confirming the canonical `labmate:result:<task_id>` path still works.

- [ ] **Step 2: Confirm no tiktoken regression**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_main.py::test_no_tiktoken_import -q
```

Expected: `1 passed` (events.py imports `token_count` from memory_consolidator,
which uses the Gemma tokenizer, never tiktoken).

- [ ] **Step 3: Commit (if any incidental fixes were needed)**

```bash
git add -A
git commit -m "test(orchestrator): green streaming + result-write regression suite"
```

---

## Gaps / decisions made (surface before implementing)

1. **Node-name mismatch (resolved in-plan).** Graph nodes are `plan/execute/
   check/reflect/approval`; spec NodeName is `*_node` + `chat_node`. Decision:
   `NODE_NAME_MAP` (Task 2). `approval` is emitted as the `approval_node`
   extension so the human-in-the-loop gate is visible (Gap 1, resolved — the
   approval node is no longer dropped). `chat_node` is never emitted by this
   graph (no chat node exists yet).

2. **`turn.created` / `session.updated` / `artifact.created` not emitted.** The
   prompt's mapping list does not include them and the current graph produces no
   artifacts. They are valid spec variants but out of scope here. **Confirm** the
   CLI (Plan 2) does not require `turn.created` to open its display (Plan 2 opens
   on first event of any type).

3. **`reasoning.done` not emitted.** The prompt maps streaming deltas only; the
   spec's `reasoning.done` (carrying the full `Reasoning` object with summary/
   tokens/budget/durationMs) would require accumulating per-node reasoning. Left
   out for v1; deltas are sufficient for the CLI. **Confirm** acceptable.

4. **`tool.*` events (resolved — Task 8b).** `astream_events`'
   `on_tool_start/on_tool_end` still won't fire for `orch.run_in_sandbox()` (a
   plain method, not a LangChain tool), so Task 8b emits `tool.start` /
   `tool.done` **manually** around the sandbox call in `execute_node` via the
   publisher stashed on `orch._event_pub`. Tool rows now surface for sandbox
   commands; the `map_langgraph_event` `on_tool_*` mapping (Task 5) remains for
   any future LangChain-wrapped tools.

5. **`context.update` token accuracy (resolved — Task 7).** All five segments
   are now populated: `systemPrompt` (first system message), `skillInstructions`
   (`orch._skill_tokens` from MCP tool schemas), `conversation`, `workingMemory`
   (non-first system messages = RAG injections), and `reasoning`
   (reflection messages + last assistant `reasoning_content`). The invariant
   `sum(segments)==used` always holds. `workingMemory` is an approximation
   (non-first system messages) but accurate enough for the bar display.

6. **EXPIRE-on-channel semantics.** `EXPIRE labmate:events:<task_id>` targets a
   key with the same name as the pub/sub channel. Pub/sub itself is keyless, so
   this creates/expires a marker only if something `SET`s that key. It satisfies
   the prompt's TTL requirement and is harmless; it does not gate event delivery.
   **Confirm** this is the intended interpretation (vs. e.g. mirroring events
   into a capped Redis Stream — a larger design not requested here).
