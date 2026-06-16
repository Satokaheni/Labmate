# 03 — Python Orchestrator Service

**Project:** Labmate  
**Status:** Implementation Plan (M3+)  
**Depends on:** `02_memory_layer.md`, `00_contracts.md`  
**Do not modify:** `core/orchestrator.py` (M2 prototype — left intact)

---

## 1. What This Service Does

The orchestrator is the cognitive spine of Labmate. It owns every decision between "user issues a task" and "agent emits a finished result."

Concretely, it does five things:

1. **Runs the LangGraph StateGraph** — a durable, crash-resumable state machine whose nodes implement the Plan → Execute → Check → Reflect → Approval loop. Every node transition is checkpointed to MongoDB so a restart resumes from the last completed super-step.

2. **Calls vLLM for LLM inference** — streaming chat completions over the OpenAI-compatible HTTP API (Contract A). Two models are used in separate calls: Gemma 4 MoE 4-bit for planning/reflection/aggregation (architect role) and Qwen2.5-Coder-32B for code generation and file edits (editor role).

3. **Dispatches tool calls via the Python MCP client** — it holds a single long-lived `ClientSession` connected to the TypeScript MCP Bridge over stdio (Contract B). Tool calls from the LLM are parsed from streaming chunks, dispatched through the session, and their results are appended to the message list before the next LLM call.

4. **Manages the Goal Tree** — a `dict[str, Goal]` stored inside the LangGraph `State`. The plan node decomposes the root goal into child goals; the execute node runs them; the check node marks them complete or failed; the reflect node resets failed goals for retry.

5. **Fans out parallel subtasks** — when the Goal Tree has multiple ready leaves, `AsyncOrchestrator.plan_and_dispatch()` runs them concurrently inside `asyncio.TaskGroup`, gated by a `Semaphore` (VRAM capacity) and `AsyncLimiter` (RPM/TPM).

What it does NOT do: it does not own the TypeScript MCP Bridge process (that is a separate container), it does not implement skills (those live in skill subprocesses), and it does not modify `core/orchestrator.py`.

---

## 2. Dependencies

### Runtime services (must be up before the orchestrator starts)

| Service | How orchestrator reaches it | Contract |
|---|---|---|
| vLLM (host) | `http://host.docker.internal:8000/v1` (`$INFERENCE_URL`) | A |
| TypeScript MCP Bridge | `stdio` — spawned as a subprocess by `MCPClientManager` | B |
| MongoDB (`lm-mongodb:27017`) | `motor.AsyncIOMotorClient` via memory layer | C |
| Redis (`lm-redis:6379`) | `redis.asyncio.Redis` via memory layer | E |
| Chroma (`lm-chroma:8000`) | `chromadb.AsyncHttpClient` via memory layer | D |

### Python packages (add to `requirements.txt`)

```
langgraph>=0.2
langgraph-checkpoint-mongodb
openai>=1.30          # AsyncOpenAI client — OpenAI-compat with vLLM
mcp>=1.27,<2          # Python MCP SDK; pin below v2 (breaking API 2026-06-30)
anyio>=4.9            # cancel-scope primitives used by mcp SDK
motor>=3.4            # async MongoDB driver
redis[asyncio]>=5.0
chromadb>=0.5
aiolimiter>=1.1       # AsyncLimiter for RPM/TPM gating
tenacity>=8.2         # retry with exponential backoff
tiktoken>=0.7         # token pre-estimation for budget gating
gitpython>=3.1        # git checkpoint after each file edit
docker>=7.0           # Docker sandbox execution
pydantic>=2.0
```

### The memory layer module (`services/orchestrator/memory/`)

Imported as `from memory.mongo import MongoMemory`, `from memory.redis import RedisMemory`, etc. See `02_memory_layer.md` for its full interface. The orchestrator never creates raw database clients directly — it always goes through the memory layer.

---

## 3. File Structure

```
services/orchestrator/
├── Dockerfile
├── requirements.txt
├── main.py               — entry point: asyncio.run(main()); signal handlers; startup sequence
├── orchestrator.py       — CodingOrchestrator class + build_graph() factory
├── mcp_client.py         — MCPClientManager (owning-task pattern; single ClientSession)
├── inference.py          — InferenceClient (AsyncOpenAI wrapper; streaming + tool accumulation)
├── types.py              — State TypedDict, Goal TypedDict, Status enum, helper functions
├── memory/               — memory layer module (see 02_memory_layer.md)
│   ├── __init__.py
│   ├── mongo.py
│   ├── redis.py
│   └── chroma.py
└── tests/
    ├── test_inference.py       — unit: stream_completion, tool chunk accumulation
    ├── test_mcp_client.py      — unit: owning-task pattern, queue dispatch, circuit breaker
    ├── test_types.py           — unit: get_ready_goals, create_goal, update_status
    ├── test_graph_nodes.py     — unit: plan/execute/check/reflect nodes with mock LLM + MCP
    └── test_integration.py     — integration: full loop against live vLLM + MCP bridge
```

---

## 4. Interface Contracts

### 4.1 OpenAI chat completions request sent by the orchestrator (Contract A)

The orchestrator sends this to `$INFERENCE_URL/v1/chat/completions`. Tool definitions come from calling `MCPClientManager.tools` (populated by `session.list_tools()` after MCP connect).

```python
# The request body built in InferenceClient.stream_completion()
{
    "model": "google/gemma-4-9b-it",          # or qwen2.5-coder-32b for editor calls
    "messages": [
        {
            "role": "system",
            "content": "<system prompt with active skills injected>"
        },
        {
            "role": "user",
            "content": "Write a function that sorts a list of dicts by a key."
        },
        # After a tool call, the assistant message and tool result are appended:
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_abc123",
                    "type": "function",
                    "function": {
                        "name": "repo_map",
                        "arguments": "{\"path\": \"/workspace\"}"
                    }
                }
            ]
        },
        {
            "role": "tool",
            "tool_call_id": "call_abc123",
            "content": "<tool result JSON string>"
        }
    ],
    "tools": [
        # One entry per tool from MCP session.list_tools().
        # Convert mcp.types.Tool -> OpenAI tool definition:
        {
            "type": "function",
            "function": {
                "name": "repo_map",
                "description": "Generate a ranked symbol map of a code repository",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute path to repo"},
                        "max_tokens": {"type": "integer", "default": 8192}
                    },
                    "required": ["path"]
                }
            }
        }
    ],
    "tool_choice": "auto",
    "stream": True,
    "temperature": 0.2,
    "max_tokens": 4096
}
```

### 4.2 How a streamed tool_calls chunk is parsed and dispatched

The LLM sends tool arguments split across multiple SSE chunks. Each chunk may carry a partial `arguments` string. The orchestrator accumulates them by `index` before dispatching.

```
SSE stream from vLLM:
  chunk 1: delta.tool_calls[0] = {index:0, id:"call_abc123", type:"function", function:{name:"repo_map", arguments:""}}
  chunk 2: delta.tool_calls[0] = {index:0, function:{arguments:"{\"path\":"}}
  chunk 3: delta.tool_calls[0] = {index:0, function:{arguments:"\"/workspace\"}"}}
  chunk 4: finish_reason = "tool_calls"

After accumulation (keyed by index):
  accumulated[0] = {
      id: "call_abc123",
      name: "repo_map",
      arguments: "{\"path\": \"/workspace\"}"   # fully assembled
  }

Then:
  args = json.loads(accumulated[0]["arguments"])
  result = await mcp_manager.call_tool("repo_map", args)
  # result is mcp.types.CallToolResult; extract text:
  result_text = result.content[0].text

Append to messages BEFORE next LLM call:
  messages.append({
      "role": "assistant",
      "content": None,
      "tool_calls": [{
          "id": "call_abc123",
          "type": "function",
          "function": {"name": "repo_map", "arguments": "{\"path\": \"/workspace\"}"}
      }]
  })
  messages.append({
      "role": "tool",
      "tool_call_id": "call_abc123",
      "content": result_text
  })
```

### 4.3 LangGraph State TypedDict (Contract G)

```python
from __future__ import annotations
from enum import Enum
from typing import Annotated, Dict, List, Optional
from operator import add
from typing_extensions import TypedDict


class GoalStatus(str, Enum):
    PENDING            = "pending"
    IN_PROGRESS        = "in_progress"
    COMPLETED          = "completed"
    FAILED             = "failed"
    BLOCKED            = "blocked"
    AWAITING_APPROVAL  = "awaiting_approval"


class Goal(TypedDict):
    id:          str
    description: str
    status:      str           # GoalStatus value; str for JSON safety
    parent_id:   Optional[str]
    children:    List[str]     # child goal IDs
    result:      Optional[str]
    error:       Optional[str]
    attempts:    int
    started_at:  Optional[str] # ISO-8601
    updated_at:  Optional[str] # ISO-8601


class State(TypedDict):
    session_id:        str
    messages:          Annotated[List[dict], add]  # reducer: parallel nodes append safely
    goals:             Dict[str, Goal]             # goal_id -> Goal
    root_goal_id:      str
    active_tool_calls: List[dict]                  # in-flight tool_calls from current LLM response
    iteration:         int
    token_count:       int
    next_action:       str    # "plan"|"execute"|"check"|"reflect"|"done"|"approval"
    error:             Optional[str]
    step_markers:      Dict[str, str]              # step_id -> "started"|"completed" (idempotency)
```

**Critical rule:** every value in `State` must survive `json.dumps()` / `json.loads()`. No Python objects, no DB clients, no coroutines, no datetimes (use ISO-8601 strings). Violating this corrupts the MongoDB checkpoint silently.

### 4.4 How State maps to MongoDB documents (Contract C)

```
LangGraph State                         MongoDB (collection: sessions)
─────────────────────────────────────── ──────────────────────────────────────
state["session_id"]              →      sessions.session_id (unique index)
state["goals"][root_id]["description"]  sessions.goal (original user request)
state["goals"]                   →      sessions.goal_tree (nested doc)
state["next_action"]             →      sessions.status derived field
──────────────────────────────────────────────────────────────────────────────

LangGraph State                         MongoDB (collection: messages)
─────────────────────────────────────── ──────────────────────────────────────
state["messages"][n]             →      messages.{role, content, tool_calls, ...}
                                        messages.session_id = state["session_id"]
                                        messages.sequence = n (position in list)

AsyncMongoDBSaver also writes to:       MongoDB (collection: checkpoints)
  — Full state snapshot at every super-step (auto-managed by LangGraph)
  — Keyed by thread_id = session_id
  — Resume: graph.ainvoke(None, {"configurable": {"thread_id": session_id}})
```

---

## 5. Implementation Steps

Build in this exact order. Each step produces a testable unit before the next depends on it.

### Step 1 — `inference.py`: InferenceClient

No MCP dependency. Just the OpenAI async client wrapper with tool call chunk accumulation.

**File:** `services/orchestrator/inference.py`

Implement `InferenceClient` with:
- `__init__(self, base_url: str, model: str, api_key: str = "EMPTY")` — construct `openai.AsyncOpenAI(base_url=base_url, api_key=api_key)`
- `async def stream_completion(self, messages: list[dict], tools: list[dict] | None = None) -> tuple[str, list[dict]]` — streams the response; returns `(text_content, tool_calls)` where `tool_calls` is a list of fully assembled `{id, name, arguments_str}` dicts after accumulation across chunks (see Section 6 for the accumulation pattern)
- `def mcp_tools_to_openai(self, mcp_tools: list) -> list[dict]` — converts `mcp.types.Tool` objects to OpenAI tool definitions

**Test:** `tests/test_inference.py` — mock `AsyncOpenAI`, feed it canned SSE chunks containing split tool_calls, assert the accumulated result is correct.

### Step 2 — `mcp_client.py`: MCPClientManager

No LangGraph dependency. The owning-task MCP client.

**File:** `services/orchestrator/mcp_client.py`

Implement `MCPClientManager` exactly as specified in Section 6 (the owning-task pattern). Key methods:
- `async def start(self) -> None` — creates the owning `asyncio.Task`
- `async def wait_ready(self, timeout: float = 10.0) -> None`
- `async def call_tool(self, name: str, args: dict, timeout: float | None = None) -> Any`
- `async def shutdown(self) -> None`

Also implement `_RobustStdioFilter` — a wrapper around the stdio reader that silently drops non-JSON lines before they reach the MCP parser (defense against stray TS `console.log` on stdout).

**Test:** `tests/test_mcp_client.py` — use `asyncio.Queue` to fake the TS server; verify the owning-task invariant (no `RuntimeError` on shutdown from a different task); verify circuit breaker trips after `max_failures` crashes.

### Step 3 — `types.py`: State and Goal TypedDicts

**File:** `services/orchestrator/types.py`

Implement everything from Section 4.3 plus:

```python
import datetime, uuid

def now_iso() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"

def new_goal_id() -> str:
    return str(uuid.uuid4())

def create_goal(tree: dict[str, Goal], gid: str, parent_id: str | None, desc: str) -> dict[str, Goal]:
    """Insert a new PENDING Goal and wire it into its parent's children list."""
    tree[gid] = Goal(
        id=gid, description=desc, status=GoalStatus.PENDING,
        parent_id=parent_id, children=[], result=None, error=None,
        attempts=0, started_at=None, updated_at=now_iso(),
    )
    if parent_id and parent_id in tree:
        tree[parent_id]["children"].append(gid)
    return tree

def update_goal(tree: dict[str, Goal], gid: str, **kwargs) -> dict[str, Goal]:
    """Update fields on a Goal; always sets updated_at."""
    tree[gid] = {**tree[gid], "updated_at": now_iso(), **kwargs}
    return tree

def get_ready_goals(tree: dict[str, Goal]) -> list[Goal]:
    """Return all PENDING goals whose children are all COMPLETED (eligible for execution)."""
    return [
        g for g in tree.values()
        if g["status"] == GoalStatus.PENDING
        and all(tree[c]["status"] == GoalStatus.COMPLETED for c in g["children"])
    ]

def all_done(tree: dict[str, Goal]) -> bool:
    """True when every goal is COMPLETED or FAILED (no more work)."""
    return all(g["status"] in (GoalStatus.COMPLETED, GoalStatus.FAILED) for g in tree.values())
```

**Test:** `tests/test_types.py` — assert `get_ready_goals` returns leaves when children are complete; assert `create_goal` wires parent correctly; assert `State` round-trips through `json.dumps/loads`.

### Step 4 — Graph nodes: plan, execute, check, reflect, approval, done

**File:** `services/orchestrator/orchestrator.py` (the `make_nodes()` factory)

Each node is an `async def` that receives `State` and returns a **partial dict** of updated keys (never mutates the state in place). LangGraph merges the delta into the checkpoint.

Implement in this order:

**4a. `plan` node** — calls `InferenceClient` with the Gemma 4 model; parses the response into child Goal entries; writes them into `goals`; sets `next_action = "execute"`.

**4b. `execute` node** — reads `get_ready_goals(state["goals"])`; if multiple goals are ready, delegates to `AsyncOrchestrator.plan_and_dispatch()`; if single goal, runs the ReAct inner loop (Thought→Action→Observation using `InferenceClient` + `MCPClientManager`); includes idempotency guard via `step_markers`; calls `git_checkpoint()` on Monitor `pass`; sets `next_action = "check"`.

**4c. `check` node** — inspects the latest Observation (test output, linter, exit code); classifies as `pass | fail | continue`; updates goal status; sets `next_action` to route to reflect/approval/execute/done.

**4d. `reflect` node** — calls Gemma 4 architect with the failure log; writes the diagnosis to Redis (hot) and MongoDB (durable via memory layer); resets the goal to `PENDING` with incremented `attempts`; sets `next_action = "execute"`.

**4e. `approval` node** — calls `langgraph.types.interrupt({"action": "irreversible", "goal": gid})`; on resume, checks the decision value and either advances or blocks the goal.

**4f. `done` node** — writes the final session result to MongoDB; sets `next_action = "done"`.

### Step 5 — Graph edges + conditional router

**File:** `services/orchestrator/orchestrator.py` (the `build_graph()` factory)

```
START → plan → execute → check →[router]→ reflect → execute
                                        → approval → execute
                                        → execute  (next sibling ready)
                                        → done     (all goals terminal)
```

The router reads `state["next_action"]` and `state["goals"]`. It must read only values committed in a **prior super-step** — never values written in the current super-step (this is a known LangGraph pitfall: the router sees the state as it was before the `check` node ran if `check` is in the same super-step).

```python
def router(state: State) -> str:
    action = state.get("next_action", "done")
    if action == "reflect":   return "reflect"
    if action == "approval":  return "approval"
    if action == "execute":   return "execute"
    return "done"
```

### Step 6 — AsyncMongoDBSaver checkpointer wiring

**File:** `services/orchestrator/orchestrator.py` (inside `build_graph()`)

The checkpointer must be created inside an `async with` block and kept alive for the graph's lifetime. Do NOT create it inside the `build_graph` function and return it — the context manager would exit and close the MongoDB connection.

```python
async def build_graph(mongo_uri: str, db_name: str, ...):
    # Returns (graph, checkpointer_context_manager)
    # The CALLER is responsible for keeping the async with block open.
    # See Step 7 for how main.py owns the lifetime.
    ...
```

Pattern: `main.py` opens `async with AsyncMongoDBSaver.from_conn_string(...) as cp:` and calls `build_graph(cp, ...)` inside that block.

Call `await cp.setup()` once at startup — it creates MongoDB indexes idempotently.

### Step 7 — `CodingOrchestrator.run()` entry point

**File:** `services/orchestrator/orchestrator.py`

`CodingOrchestrator` wraps the compiled graph. Its `run()` method constructs the initial `State` and calls `await graph.ainvoke(initial_state, config)`.

For resume after crash: `await graph.ainvoke(None, config)` — passing `None` tells LangGraph to load the latest checkpoint for `thread_id`.

```python
class CodingOrchestrator:
    def __init__(self, graph, mcp_manager: MCPClientManager, inference: InferenceClient, memory):
        self.graph       = graph
        self.mcp         = mcp_manager
        self.inference   = inference
        self.memory      = memory

    async def run(self, task: str, session_id: str) -> State:
        """Start a new session. Returns final State."""
        from types import create_goal, GoalStatus
        root_id = "root"
        initial: State = {
            "session_id":        session_id,
            "messages":          [],
            "goals":             create_goal({}, root_id, None, task),
            "root_goal_id":      root_id,
            "active_tool_calls": [],
            "iteration":         0,
            "token_count":       0,
            "next_action":       "plan",
            "error":             None,
            "step_markers":      {},
        }
        config = {"configurable": {"thread_id": session_id}}
        return await self.graph.ainvoke(initial, config)

    async def resume(self, session_id: str) -> State:
        """Resume a crashed session. Loads last checkpoint automatically."""
        config = {"configurable": {"thread_id": session_id}}
        return await self.graph.ainvoke(None, config)
```

### Step 8 — `main.py` with signal handlers

**File:** `services/orchestrator/main.py`

```python
import asyncio, os, signal, logging
from langgraph.checkpoint.mongodb.aio import AsyncMongoDBSaver

from inference import InferenceClient
from mcp_client import MCPClientManager
from orchestrator import CodingOrchestrator, build_graph
from mcp import StdioServerParameters

log = logging.getLogger(__name__)

async def main():
    mongo_uri   = os.environ["MONGO_URI"]      # mongodb://mongodb:27017/labmate
    inf_url     = os.environ["INFERENCE_URL"]  # http://host.docker.internal:8000
    mcp_cmd     = os.environ["MCP_BRIDGE_CMD"] # node /app/dist/index.js

    # 1. Start MCP client (owning task)
    mcp_params = StdioServerParameters(command="node", args=["/app/dist/index.js"])
    mcp_mgr    = MCPClientManager(mcp_params)
    await mcp_mgr.start()
    await mcp_mgr.wait_ready(timeout=15.0)
    log.info("MCP bridge ready; tools: %s", [t.name for t in mcp_mgr.tools])

    # 2. Build InferenceClient
    inference = InferenceClient(base_url=inf_url, model="google/gemma-4-9b-it")

    # 3. Wire checkpointer + graph (checkpointer context kept open for app lifetime)
    async with AsyncMongoDBSaver.from_conn_string(mongo_uri) as cp:
        await cp.setup()
        graph = build_graph(cp, mcp_mgr, inference)
        orch  = CodingOrchestrator(graph, mcp_mgr, inference, memory=None)

        # 4. Signal handlers for clean shutdown
        loop = asyncio.get_running_loop()
        shutdown_event = asyncio.Event()

        def _signal_handler(sig):
            log.info("Received %s, shutting down", sig)
            shutdown_event.set()

        loop.add_signal_handler(signal.SIGTERM, lambda: _signal_handler("SIGTERM"))
        loop.add_signal_handler(signal.SIGINT,  lambda: _signal_handler("SIGINT"))

        # 5. Main work loop (replace with Discord/HTTP interface as needed)
        log.info("Orchestrator ready")
        await shutdown_event.wait()

    # Cleanup
    await mcp_mgr.shutdown()
    log.info("Orchestrator stopped")

if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
    asyncio.run(main())
```

---

## 6. Key Code Patterns

### Pattern A — MCPClientManager owning-task pattern

This is the only correct way to hold a long-lived MCP `ClientSession`. The context managers must enter AND exit in the same `asyncio.Task`.

```python
# services/orchestrator/mcp_client.py
from __future__ import annotations

import asyncio, random, time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters, stdio_client


class CircuitOpenError(Exception):
    """Circuit breaker has tripped; MCP server crashed too many times."""


@dataclass
class _Req:
    name:    str
    args:    dict[str, Any]
    future:  asyncio.Future
    timeout: float = 30.0


class MCPClientManager:
    """
    INVARIANT: stdio_client() and ClientSession() are entered AND exited
    inside _run(), which runs in ONE dedicated asyncio.Task created by start().
    Callers never touch the session — they enqueue _Req objects and await the
    associated Future. This is the only pattern that satisfies anyio's
    cancel-scope rule. See python-sdk issues #79, #252, #521, #577.
    """

    def __init__(
        self,
        params: StdioServerParameters,
        *,
        max_failures: int  = 5,
        window:       float = 60.0,
        call_timeout: float = 30.0,
    ) -> None:
        self._params        = params
        self._inbox:        asyncio.Queue[_Req] = asyncio.Queue()
        self._ready         = asyncio.Event()
        self._task:         asyncio.Task | None = None
        self._failures:     deque[float] = deque()
        self._max_failures  = max_failures
        self._window        = window
        self._call_timeout  = call_timeout
        self.tools:         list = []

    async def start(self) -> None:
        """Create the single owning lifecycle task. Call once at startup."""
        self._task = asyncio.create_task(self._run(), name="mcp-lifecycle")

    async def wait_ready(self, timeout: float = 10.0) -> None:
        await asyncio.wait_for(self._ready.wait(), timeout=timeout)

    async def call_tool(self, name: str, args: dict, timeout: float | None = None) -> Any:
        """
        Submit a tool call from ANY coroutine. Many callers can call this
        concurrently — they all share the single session via the Queue.
        """
        fut = asyncio.get_running_loop().create_future()
        await self._inbox.put(_Req(name, args, fut, timeout or self._call_timeout))
        return await fut

    async def shutdown(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    # ------------------------------------------------------------------ internal

    def _breaker_open(self) -> bool:
        now = time.monotonic()
        while self._failures and now - self._failures[0] > self._window:
            self._failures.popleft()
        return len(self._failures) >= self._max_failures

    def _drain_inbox(self, exc: Exception) -> None:
        while not self._inbox.empty():
            try:
                req = self._inbox.get_nowait()
                if not req.future.done():
                    req.future.set_exception(exc)
            except asyncio.QueueEmpty:
                break

    async def _run(self) -> None:
        """
        The owning task. stdio_client() and ClientSession() context managers
        are entered AND exited here — in this single task — always.
        """
        backoff = 0.5
        while True:
            if self._breaker_open():
                err = CircuitOpenError(
                    f"MCP bridge crashed {self._max_failures}+ times in {self._window}s"
                )
                self._drain_inbox(err)
                await asyncio.sleep(self._window)
                self._failures.clear()
                continue

            try:
                async with stdio_client(self._params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.list_tools()  # always refresh after connect
                        self.tools = result.tools
                        self._ready.set()
                        backoff = 0.5                        # reset on successful connect
                        await self._serve(session)

            except asyncio.CancelledError:
                return  # clean shutdown; context managers exit here in this task

            except Exception:
                self._failures.append(time.monotonic())
                self._ready.clear()
                jitter = random.uniform(0, backoff)
                await asyncio.sleep(backoff + jitter)
                backoff = min(backoff * 2, 30.0)
                # context managers exit here, in this same task — no RuntimeError

    async def _serve(self, session: ClientSession) -> None:
        """Multiplex inbox requests onto the session sequentially."""
        while True:
            req = await self._inbox.get()
            try:
                with anyio.fail_after(req.timeout):
                    result = await session.call_tool(req.name, req.args)
                if not req.future.done():
                    req.future.set_result(result)
            except TimeoutError as exc:
                if not req.future.done():
                    req.future.set_exception(exc)
            except Exception as exc:
                if not req.future.done():
                    req.future.set_exception(exc)
                raise  # bubble up to _run() to trigger reconnect
```

### Pattern B — InferenceClient.stream_completion() with tool call accumulation

Tool call arguments arrive fragmented across many SSE chunks. Accumulate by `index` into a dict before parsing JSON.

```python
# services/orchestrator/inference.py
from __future__ import annotations
import json
from typing import Any
import openai


class InferenceClient:
    def __init__(self, base_url: str, model: str, api_key: str = "EMPTY") -> None:
        self._model  = model
        self._client = openai.AsyncOpenAI(base_url=base_url, api_key=api_key)

    async def stream_completion(
        self,
        messages: list[dict],
        tools:    list[dict] | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        """
        Stream a chat completion. Returns:
          text_content: str — the assistant's text (empty string if tool call)
          tool_calls:   list of {id, name, arguments_str} dicts — fully assembled

        CRITICAL: tool call arguments arrive split across multiple chunks.
        You MUST accumulate by delta.tool_calls[n].index before parsing JSON.
        Parsing each chunk's partial arguments string will raise json.JSONDecodeError.
        """
        kwargs: dict[str, Any] = {
            "model":    self._model,
            "messages": messages,
            "stream":   True,
        }
        if tools:
            kwargs["tools"]       = tools
            kwargs["tool_choice"] = "auto"

        # Accumulators
        text_parts: list[str]        = []
        # accumulated[index] = {id, name, arguments_parts: [str, ...]}
        accumulated: dict[int, dict] = {}

        stream = await self._client.chat.completions.create(**kwargs)
        async for chunk in stream:
            delta = chunk.choices[0].delta

            # Accumulate text
            if delta.content:
                text_parts.append(delta.content)

            # Accumulate tool call fragments
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in accumulated:
                        accumulated[idx] = {"id": "", "name": "", "arg_parts": []}
                    if tc.id:
                        accumulated[idx]["id"]   = tc.id
                    if tc.function and tc.function.name:
                        accumulated[idx]["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        accumulated[idx]["arg_parts"].append(tc.function.arguments)

        # Assemble fully accumulated tool calls
        tool_calls: list[dict] = []
        for idx in sorted(accumulated):
            acc = accumulated[idx]
            tool_calls.append({
                "id":            acc["id"],
                "name":          acc["name"],
                "arguments_str": "".join(acc["arg_parts"]),  # full JSON string, safe to parse now
            })

        return "".join(text_parts), tool_calls

    def mcp_tools_to_openai(self, mcp_tools: list) -> list[dict]:
        """Convert mcp.types.Tool list to OpenAI tool definitions."""
        result = []
        for t in mcp_tools:
            result.append({
                "type": "function",
                "function": {
                    "name":        t.name,
                    "description": t.description or "",
                    "parameters":  t.inputSchema,
                }
            })
        return result
```

### Pattern C — LangGraph graph factory with AsyncMongoDBSaver

```python
# services/orchestrator/orchestrator.py (excerpt)
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.mongodb.aio import AsyncMongoDBSaver
from langgraph.types import interrupt
from types import State

def build_graph(checkpointer, mcp_mgr, inference):
    """
    Build and compile the StateGraph.
    `checkpointer` is an already-open AsyncMongoDBSaver instance — the CALLER
    owns the async with block that keeps it alive. Do not open/close it here.
    """
    plan, execute, check, reflect, approval, done = make_nodes(mcp_mgr, inference)

    b = StateGraph(State)
    b.add_node("plan",     plan)
    b.add_node("execute",  execute)
    b.add_node("check",    check)
    b.add_node("reflect",  reflect)
    b.add_node("approval", approval)
    b.add_node("done",     done)

    b.add_edge(START, "plan")
    b.add_edge("plan",    "execute")
    b.add_edge("execute", "check")
    b.add_conditional_edges("check", router, ["execute", "reflect", "approval", "done"])
    b.add_edge("reflect",  "execute")
    b.add_edge("approval", "execute")
    b.add_edge("done",     END)

    return b.compile(checkpointer=checkpointer)
```

### Pattern D — The execute node: LLM tool call → MCP dispatch → result → next LLM call

This is the bridge between the LLM and the MCP tools. It runs the ReAct inner loop for a single goal.

```python
# Inside make_nodes() in orchestrator.py

async def execute(state: State) -> dict:
    from types import get_ready_goals, update_goal, GoalStatus, now_iso

    ready = get_ready_goals(state["goals"])
    if not ready:
        return {"next_action": "done"}

    goal     = ready[0]           # single goal path; parallel handled by AsyncOrchestrator
    gid      = goal["id"]
    markers  = dict(state["step_markers"])

    # Idempotency guard
    if markers.get(f"{gid}:execute") == "completed":
        return {"next_action": "check"}

    markers[f"{gid}:execute"] = "started"
    goals = update_goal(dict(state["goals"]), gid,
                        status=GoalStatus.IN_PROGRESS, started_at=now_iso())

    # Build the message list for the execute call (include goal context)
    messages = list(state["messages"]) + [{
        "role":    "user",
        "content": f"Execute this subtask: {goal['description']}"
    }]

    # Convert MCP tools to OpenAI format
    openai_tools = inference.mcp_tools_to_openai(mcp_mgr.tools)

    max_react_iter = 5
    for _ in range(max_react_iter):
        text, tool_calls = await inference.stream_completion(messages, openai_tools)

        if not tool_calls:
            # LLM produced a text response — treat as completion
            goals = update_goal(goals, gid,
                                status=GoalStatus.COMPLETED, result=text,
                                updated_at=now_iso())
            markers[f"{gid}:execute"] = "completed"
            messages.append({"role": "assistant", "content": text})
            break

        # LLM wants to call tools — dispatch and append results
        # Build the assistant message with tool_calls
        oai_tool_calls = [
            {
                "id":   tc["id"],
                "type": "function",
                "function": {"name": tc["name"], "arguments": tc["arguments_str"]}
            }
            for tc in tool_calls
        ]
        messages.append({"role": "assistant", "content": None, "tool_calls": oai_tool_calls})

        # Fan out tool calls (may be >1 in one response)
        # For parallel calls: asyncio.TaskGroup with semaphore; see Pattern E
        for tc in tool_calls:
            args   = json.loads(tc["arguments_str"])
            result = await mcp_mgr.call_tool(tc["name"], args)

            # Extract text from CallToolResult.content
            result_text = result.content[0].text if result.content else ""

            # CRITICAL: append tool result before next LLM call
            messages.append({
                "role":         "tool",
                "tool_call_id": tc["id"],
                "content":      result_text,
            })

    return {
        "goals":        goals,
        "messages":     messages,
        "step_markers": markers,
        "next_action":  "check",
    }
```

### Pattern E — asyncio.TaskGroup fan-out for parallel tool calls

When the LLM returns multiple tool calls in one response (or when `get_ready_goals` returns multiple goals), fan them out concurrently with a `Semaphore` acquired inside the worker, not at the dispatch site.

```python
import asyncio

async def fan_out_tool_calls(
    tool_calls: list[dict],
    mcp_mgr,
    semaphore: asyncio.Semaphore,
) -> list[tuple[str, str]]:
    """
    Dispatch multiple tool calls concurrently.
    Returns list of (tool_call_id, result_text) in order.

    Semaphore is acquired INSIDE the worker, not at the fan-out site.
    Acquiring at fan-out site can deadlock when all permits are held by
    parents waiting on children that cannot start.
    """
    results: dict[str, str] = {}

    async def worker(tc: dict) -> None:
        async with semaphore:                      # acquired inside the leaf
            args        = json.loads(tc["arguments_str"])
            call_result = await mcp_mgr.call_tool(tc["name"], args)
            results[tc["id"]] = call_result.content[0].text if call_result.content else ""

    async with asyncio.TaskGroup() as tg:          # auto-cancels siblings on failure
        for tc in tool_calls:
            tg.create_task(worker(tc))

    # Preserve original order
    return [(tc["id"], results[tc["id"]]) for tc in tool_calls]
```

### Pattern F — Full bridge walkthrough: LLM says "call repo_map" → result

```
1. InferenceClient.stream_completion() returns:
       tool_calls = [{"id": "call_abc123", "name": "repo_map",
                      "arguments_str": "{\"path\": \"/workspace\"}"}]

2. Execute node parses the tool call:
       args = json.loads('{"path": "/workspace"}')   → {"path": "/workspace"}

3. Execute node appends the assistant message (BEFORE dispatching):
       messages.append({
           "role": "assistant",
           "content": None,
           "tool_calls": [{"id": "call_abc123", "type": "function",
                           "function": {"name": "repo_map",
                                        "arguments": "{\"path\": \"/workspace\"}"}}]
       })

4. MCPClientManager.call_tool("repo_map", {"path": "/workspace"}) dispatches via the
   owning task's _serve() loop → ClientSession.call_tool() → TS MCP Bridge stdin

5. TS MCP Bridge processes the call → returns CallToolResult on stdout:
       result.content[0].text = '{"symbols": [{"name": "sort_dicts", ...}]}'
       result.isError = False

6. Execute node appends the tool result (BEFORE next LLM call):
       messages.append({
           "role": "tool",
           "tool_call_id": "call_abc123",
           "content": '{"symbols": [{"name": "sort_dicts", ...}]}'
       })

7. Execute node calls InferenceClient.stream_completion(messages, tools) again.
   The LLM now sees the tool result and continues reasoning.

8. If the LLM responds with plain text (no more tool_calls), the execute node
   marks the goal COMPLETED and exits the ReAct loop.
```

---

## 7. Integration Verification

Run these commands in order after implementing all steps. All require the MCP bridge and vLLM to be running.

### 7.1 Health check vLLM

```bash
curl http://localhost:8000/health
# Expected: {"status": "ok"}

curl http://localhost:8000/v1/models
# Expected: {"data": [{"id": "google/gemma-4-9b-it", ...}]}
```

### 7.2 Smoke test MCP bridge in isolation

```bash
# Start the TS MCP bridge manually and send a tools/list
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"clientInfo":{"name":"test","version":"1.0.0"}}}' \
  | node services/mcp-bridge/dist/index.js

# Expected on stdout (one line): {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05",...}}
# Nothing else on stdout — logs go to stderr only
```

### 7.3 Unit tests

```bash
cd services/orchestrator
python -m pytest tests/test_inference.py tests/test_mcp_client.py tests/test_types.py -v
# Expected: all tests pass
```

### 7.4 End-to-end: "list files in /workspace"

```python
# Run this script: python e2e_smoke.py
import asyncio, os
from mcp import StdioServerParameters
from mcp_client import MCPClientManager
from inference import InferenceClient
from orchestrator import CodingOrchestrator, build_graph
from langgraph.checkpoint.mongodb.aio import AsyncMongoDBSaver

async def smoke():
    mcp_params = StdioServerParameters(command="node", args=["services/mcp-bridge/dist/index.js"])
    mcp_mgr    = MCPClientManager(mcp_params)
    await mcp_mgr.start()
    await mcp_mgr.wait_ready(timeout=10.0)
    print("Tools:", [t.name for t in mcp_mgr.tools])

    inference = InferenceClient(base_url="http://localhost:8000", model="google/gemma-4-9b-it")

    async with AsyncMongoDBSaver.from_conn_string("mongodb://localhost:27017", db_name="labmate") as cp:
        await cp.setup()
        graph = build_graph(cp, mcp_mgr, inference)
        orch  = CodingOrchestrator(graph, mcp_mgr, inference, memory=None)

        import uuid
        final_state = await orch.run("List the files in /workspace", session_id=str(uuid.uuid4()))

    await mcp_mgr.shutdown()
    print("Final messages:")
    for m in final_state["messages"]:
        print(f"  [{m['role']}]", m.get("content", "")[:120])
    print("Goals:")
    for g in final_state["goals"].values():
        print(f"  {g['id']}: {g['status']} — {g.get('result', '')[:80]}")

asyncio.run(smoke())
```

Expected output:
```
Tools: ['fs_read_file', 'fs_list_dir', 'git_status', 'repo_map', ...]
Final messages:
  [user] List the files in /workspace
  [assistant] None          (tool call to fs_list_dir or repo_map)
  [tool] {"files": [...]}
  [assistant] The files in /workspace are: ...
Goals:
  root: completed — The files in /workspace are: main.py, ...
```

### 7.5 Verify MongoDB checkpoint was written

```bash
mongosh "mongodb://localhost:27017/labmate" --eval \
  "db.checkpoints.find({}, {thread_id:1, ts:1}).sort({ts:-1}).limit(3).pretty()"
# Expected: 3 recent checkpoint documents with the session_id you just ran
```

### 7.6 Verify resume after crash

```python
# Run orch.resume(session_id) with the same session_id from 7.4
# Kill the process mid-run (Ctrl+C after "plan" node), then rerun with resume()
final_state = await orch.resume(session_id=<session_id from 7.4>)
# Expected: LangGraph loads the checkpoint; plan node is NOT re-run;
# execution continues from where it was interrupted
```

---

## 8. Done Criteria

The orchestrator implementation is complete when all of the following are true:

- [ ] `python -m pytest services/orchestrator/tests/ -v` passes with no skips
- [ ] `tests/test_mcp_client.py` proves the owning-task invariant: `MCPClientManager.shutdown()` called from a different task raises no `RuntimeError`
- [ ] `tests/test_inference.py` proves tool call chunk accumulation: a canned stream with arguments split across 5 chunks produces the correct assembled `arguments_str`
- [ ] The smoke script in Section 7.4 completes the "list files in /workspace" task end-to-end, calling at least one MCP tool and returning a text result
- [ ] `db.checkpoints.find(...)` in MongoDB shows a checkpoint after every node (verify by counting documents before and after a run)
- [ ] Resume after a simulated crash (kill after plan, resume) continues from the last completed node without re-running it
- [ ] `tests/test_graph_nodes.py` verifies the idempotency guard: calling the `execute` node twice with `step_markers[gid] = "completed"` returns early without calling the LLM a second time
- [ ] `core/orchestrator.py` is untouched (git diff confirms no modifications)
- [ ] No Python object, coroutine, or DB client appears in any `State` key (verified by round-tripping the final state through `json.dumps`/`json.loads`)

---

## 9. Common Mistakes

### CRITICAL — will cause a silent broken session

**Returning ClientSession from an async function (anyio cancel scope violation)**

```python
# WRONG — enters stdio_client() in task A, exits it in task B
class Bad:
    async def setup(self):
        self._stack   = AsyncExitStack()
        r, w          = await self._stack.enter_async_context(stdio_client(params))
        self._session = await self._stack.enter_async_context(ClientSession(r, w))

    async def teardown(self):               # called from a different task
        await self._stack.aclose()          # RuntimeError: cancel scope in wrong task

# CORRECT — entire lifecycle inside one owning task (_run)
async def _run(self):
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await self._serve(session)
    # context managers exit here, in this task
```

The `RuntimeError: Attempted to exit cancel scope in a different task than it was entered in` is the most common MCP client failure. It is architectural — you cannot patch around it. The owning-task pattern in Section 6 Pattern A is the only fix.

**Not accumulating streaming tool call chunks**

```python
# WRONG — each chunk's arguments is a partial fragment; parsing it will raise JSONDecodeError
async for chunk in stream:
    tc = chunk.choices[0].delta.tool_calls[0]
    args = json.loads(tc.function.arguments)   # JSONDecodeError — partial string like '{"path":'

# CORRECT — accumulate all chunks for the same index, then parse once
async for chunk in stream:
    tc = chunk.choices[0].delta.tool_calls[0]
    accumulated[tc.index]["arg_parts"].append(tc.function.arguments or "")

# After stream ends:
full_args_str = "".join(accumulated[0]["arg_parts"])
args = json.loads(full_args_str)   # safe — complete JSON
```

Tool call arguments can arrive split across dozens of chunks. Never parse a partial `arguments` string. Always accumulate by `index` across the entire stream before calling `json.loads`.

**Forgetting to append tool results to message history before the next LLM call**

```python
# WRONG — LLM sees its own tool_calls but no result; it hallucinates or loops
messages.append({"role": "assistant", "content": None, "tool_calls": [...]})
# ... dispatch tool call ...
text, _ = await inference.stream_completion(messages)  # missing tool result!

# CORRECT — always append BOTH the assistant tool_call message AND the tool result
messages.append({"role": "assistant", "content": None, "tool_calls": [...]})
result_text = await call_and_extract(tc)
messages.append({
    "role":         "tool",
    "tool_call_id": tc["id"],   # must match the id in the assistant message
    "content":      result_text,
})
text, _ = await inference.stream_completion(messages)  # LLM sees full context
```

The `tool_call_id` in the tool result message MUST match the `id` in the assistant's `tool_calls` entry. vLLM will reject the request or produce wrong output if they do not match.

### HIGH — will cause incorrect behavior silently

**Putting non-JSON-serializable values in State**

Any Python object (datetime, Enum instance as a Python object, DB client, coroutine) stored in `State` will serialize successfully to MongoDB via LangGraph's internal serializer but will NOT be recoverable on resume. Use only `str`, `int`, `float`, `bool`, `None`, `list`, and `dict`. For enums, store `.value` (a string). For datetimes, store as ISO-8601 string via `now_iso()`.

**Missing idempotency guard on side-effecting nodes**

LangGraph re-executes the in-flight node on crash+resume. Without the `step_markers` guard, the `execute` node makes a second LLM call, writes a second git commit, or fires a second API call. Always write `markers[step_id] = "started"` before the side effect and `markers[step_id] = "completed"` after. Short-circuit if already `"completed"`.

**Acquiring the Semaphore at the fan-out site instead of inside the worker**

```python
# WRONG — can deadlock: fan-out holds all permits, children cannot start
async with asyncio.TaskGroup() as tg:
    for tc in tool_calls:
        async with semaphore:              # acquiring here before create_task
            tg.create_task(worker(tc))

# CORRECT — semaphore acquired inside the leaf worker
async def worker(tc):
    async with semaphore:                  # acquired inside the leaf, after task is running
        result = await mcp_mgr.call_tool(tc["name"], json.loads(tc["arguments_str"]))
```

**Swallowing `asyncio.CancelledError` in a worker**

```python
# WRONG — breaks TaskGroup's structured cancellation
async def worker(tc):
    try:
        result = await mcp_mgr.call_tool(...)
    except Exception:     # catches CancelledError too
        pass

# CORRECT — only catch non-cancellation exceptions; always re-raise CancelledError
async def worker(tc):
    try:
        result = await mcp_mgr.call_tool(...)
    except asyncio.CancelledError:
        raise                # NEVER swallow — keeps TaskGroup correct
    except Exception as e:
        log.error("tool call failed: %s", e)
```

**Reading intra-super-step State values in the conditional router**

The LangGraph router reads the state as it was before the current super-step. If the `check` node and the router run in the same super-step, the router may not see the `next_action` that `check` just wrote. Structure the graph so `check` completes a super-step before the router fires (i.e., `check` is not a conditional edge source that runs in the same super-step as the upstream node).

**Calling `asyncio.run()` from inside a running coroutine**

The MCP client uses `anyio` under the hood. Never call `asyncio.run()` from a coroutine that is already running on a loop — it raises `RuntimeError: cannot be called from a running event loop`. Use `await` for all async calls. If you need to call a sync function that uses `asyncio.run()`, run it in a thread via `asyncio.to_thread()`.
