# MCP Bridge Spec (Nervous System)

**Labmate v0.1 — Engineering Specification**
**Date:** 2026-06-15
**Status:** Draft

---

## 1. Overview

The MCP Bridge is the nervous system of Labmate. It is the bidirectional communication layer that connects the Python orchestrator (Brain — Gemma 4 MoE 4-bit running under asyncio) to the TypeScript MCP server that dispatches tool calls to individual skill processes.

The bridge is composed of two cooperating components:

1. **TypeScript MCP Server** — a long-lived subprocess that exposes all registered tools over the MCP protocol. It receives JSON-RPC 2.0 requests on `stdin` and writes responses to `stdout`. It never writes anything to `stdout` except valid JSON-RPC messages.

2. **Python MCP Client Manager** — an asyncio manager class that owns the subprocess lifecycle and multiplexes concurrent tool calls from the Brain onto the single persistent `ClientSession`. It handles reconnection, timeouts, and circuit-breaking without violating anyio's cancel-scope rules.

The transport between them is **stdio JSON-RPC 2.0**. This is the correct choice for Labmate: it requires no open ports, provides OS-level process isolation, and has the lowest latency for a local Brain-to-server link.

### Scope of this document

This spec covers:
- The full data flow from Brain coroutine to skill process and back
- The TypeScript MCP server implementation: project structure, tool registration, error handling, output truncation, and graceful shutdown
- The Python MCP client implementation: the single owning task pattern, request multiplexer, supervisor with circuit breaker, and per-call timeout
- BDD test scenarios for both sides of the bridge
- The two most critical failure modes: **stdout pollution** (TypeScript side) and the **anyio cancel-scope violation** (Python side)
- Dependencies, reference papers, and SOTA improvements

---

## 2. Architecture

### 2.1 Data Flow (Python Brain → Python Client → stdio → TS Server → Skill)

```
Python Brain (asyncio coroutine)
    │
    │  await manager.submit_tool_call("fs_read_file", {"path": "/src/main.py"})
    │
    ▼
MCPClientManager._inbox (asyncio.Queue[_Req])
    │
    │  _serve() loop picks up the _Req
    │
    ▼
ClientSession.call_tool("fs_read_file", args)    [wrapped in anyio.fail_after(timeout)]
    │
    │  writes JSON-RPC 2.0 request to stdin of TS server
    │
    ▼
StdioServerTransport (TypeScript, reading stdin)
    │
    │  routes to registered handler via McpServer
    │
    ▼
registerFsTools handler (src/tools/fs.ts)
    │
    │  try { ... } catch (e) { return isError: true }     ← NEVER throws
    │
    ▼
Skill process (child process / IPC call if needed)
    │
    │  returns result to handler
    │
    ▼
McpServer writes JSON-RPC 2.0 response to stdout   ← stdout is JSON-RPC ONLY
    │
    │  Python SDK reads stdout, parses response
    │
    ▼
ClientSession resolves the awaited call_tool()
    │
    ▼
MCPClientManager._serve() sets asyncio.Future result
    │
    ▼
Python Brain coroutine receives CallToolResult
```

### 2.2 TypeScript MCP Server Structure

The TS server is a single Node.js process bootstrapped in `src/index.ts`. It uses the high-level `McpServer` class (not the low-level `Server` + `setRequestHandler` API). `McpServer` handles capability negotiation, request routing, and automatic Zod-to-JSON-Schema conversion.

Tools are organized into domain modules (`src/tools/<domain>.ts`), each exporting a `registerXxxTools(server)` function. A central registry (`src/registry.ts`) wires them all. The entry point (`src/index.ts`) only handles bootstrap, transport wiring, and graceful shutdown — it does not contain any tool definitions.

### 2.3 Python MCP Client Manager

`MCPClientManager` is the single class that owns the entire MCP session lifecycle. Its critical invariant: **the `asyncio.Task` that enters `stdio_client()` and `ClientSession()` context managers is the same task that exits them.** This is enforced by running the full connection lifecycle inside `_run()`, which executes inside one dedicated owning task created by `start()`.

Callers never touch the session directly. They call `submit_tool_call()`, which enqueues a `_Req` (containing name, args, a `Future`, and a timeout), then `await` the future. The `_serve()` loop inside the owning task drains the queue and dispatches each request through the session.

### 2.4 ASCII Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│  Python Process (asyncio event loop)                                │
│                                                                     │
│   ┌──────────────┐      await future       ┌──────────────────┐    │
│   │  Brain       │ ──submit_tool_call()──► │  asyncio.Queue   │    │
│   │  (Gemma 4)   │ ◄───────────────────── │  _Req + Future   │    │
│   └──────────────┘   future.set_result()  └────────┬─────────┘    │
│                                                     │ dequeue      │
│                              ┌──────────────────────▼──────────┐   │
│                              │  _serve() loop                   │   │
│                              │  (inside owning asyncio.Task)    │   │
│                              │  anyio.fail_after(req.timeout)   │   │
│                              └──────────────┬───────────────────┘   │
│                                             │                       │
│                              ┌──────────────▼───────────────────┐   │
│                              │  ClientSession.call_tool()        │   │
│                              │  (mcp Python SDK)                 │   │
│                              └──────────────┬───────────────────┘   │
└─────────────────────────────────────────────┼───────────────────────┘
                                              │ stdin/stdout
                                    (JSON-RPC 2.0 over stdio)
┌─────────────────────────────────────────────┼───────────────────────┐
│  TypeScript Process (Node.js event loop)    │                       │
│                                             │                       │
│                              ┌──────────────▼───────────────────┐   │
│                              │  StdioServerTransport            │   │
│                              │  (reads stdin, writes stdout)    │   │
│                              └──────────────┬───────────────────┘   │
│                                             │                       │
│                              ┌──────────────▼───────────────────┐   │
│                              │  McpServer (routing)             │   │
│                              └──────────────┬───────────────────┘   │
│                                             │                       │
│        ┌────────────┬──────────────────────►│                       │
│        │            │                       │ dispatch              │
│  ┌─────▼──┐  ┌──────▼─┐  ┌────────────┐   │                       │
│  │fs tools│  │git tools│  │exec tools  │ ◄─┘                       │
│  └────────┘  └────────┘  └─────┬──────┘                            │
│                                 │ spawn/IPC                         │
│                          ┌──────▼──────┐                            │
│                          │ skill procs │                            │
│                          └─────────────┘                            │
│                                                                     │
│  stderr ──► pino logger (NEVER stdout)                              │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Transport | stdio JSON-RPC 2.0 | No open ports, OS process isolation, lowest latency for local Brain |
| TS server API tier | `McpServer` (high-level) | Automatic Zod→JSON Schema, capability negotiation, routing built-in |
| Tool organization | Domain modules + `registerXxxTools(server)` | 20+ tools cannot live in `index.ts`; domain split prevents merge conflicts |
| Schema validation | Zod with `.strict().describe()` | Rejects unknown keys, populates JSON Schema descriptions for LLM |
| Error surface | `isError: true` result, never throw | Uncaught throws become protocol errors the Brain cannot reason about |
| Output bounding | `CHARACTER_LIMIT = 25_000` + pagination | Prevents context-window blowout from large file/git outputs |
| Python session ownership | Single owning `asyncio.Task` | anyio cancel scopes must enter and exit in the same task |
| Call multiplexing | `asyncio.Queue[_Req]` + `asyncio.Future` | Many Brain coroutines share one persistent `ClientSession` |
| Reconnection | Exponential backoff + jitter + circuit breaker | Prevents crash loops from spiraling into CPU/memory exhaustion |
| Per-call timeout | `anyio.fail_after(timeout)` inside `_serve()` | One hung tool call cannot block the entire serve loop |
| mcp SDK pin | `>=1.27,<2` | v2 beta targets 2026-06-30 with breaking API changes; pin below it |
| TS SDK pin | Exact version (e.g. `1.x.y`) | SDK and spec move fast; floating `^1.x` can pull breaking changes |

---

## 4. TypeScript MCP Server Implementation

### 4.1 Project Structure (`src/` layout)

```
src/
├── index.ts              # Bootstrap: McpServer + StdioServerTransport + graceful shutdown
├── registry.ts           # registerAllTools(server) — calls every domain registrar
├── tools/
│   ├── fs.ts             # registerFsTools(server)
│   ├── git.ts            # registerGitTools(server)
│   └── exec.ts           # registerExecTools(server)  — IPC to skill subprocesses
├── schemas/
│   ├── fs.ts             # Zod schemas for fs tools
│   ├── git.ts            # Zod schemas for git tools
│   └── exec.ts           # Zod schemas for exec tools
├── services/
│   ├── logger.ts         # pino → stderr (NEVER stdout)
│   └── ipc.ts            # Child-process MCP client to skill servers, retry/timeout
├── utils/
│   └── truncate.ts       # truncate() with has_more/next_offset pagination
├── types.ts              # z.infer-derived types
└── constants.ts          # CHARACTER_LIMIT, tool-name prefixes
```

Supporting files:

```
package.json              # "type": "module"; exact SDK version pinned
tsconfig.json             # strict: true, target: ES2022, module: Node16+
```

### 4.2 McpServer Bootstrap

```typescript
// src/index.ts
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { registerAllTools } from './registry.js';
import { log } from './services/logger.js';

async function main() {
  const server = new McpServer({ name: 'labmate', version: '0.1.0' });
  registerAllTools(server);
  const transport = new StdioServerTransport();

  let shuttingDown = false;
  const shutdown = async (sig: string) => {
    if (shuttingDown) return;
    shuttingDown = true;
    log.info({ sig }, 'shutting down');  // ← stderr, never stdout
    try {
      await server.close();
      await transport.close();
    } finally {
      process.exit(0);
    }
  };

  // Register both signals — containers (RunPod, k8s) use SIGTERM
  process.on('SIGINT',  () => void shutdown('SIGINT'));
  process.on('SIGTERM', () => void shutdown('SIGTERM'));
  process.on('uncaughtException', (e) => {
    log.fatal(e, 'uncaught');
    void shutdown('uncaughtException');
  });

  // Begins reading stdin / writing stdout — JSON-RPC ONLY from this point
  await server.connect(transport);
  log.info('labmate MCP server ready on stdio');  // ← stderr
}

main().catch((e) => { log.fatal(e, 'fatal startup'); process.exit(1); });
```

```typescript
// src/services/logger.ts
// CRITICAL: destination fd 2 = stderr. stdout is reserved for JSON-RPC.
import pino from 'pino';
export const log = pino(
  { level: process.env.LOG_LEVEL ?? 'info' },
  pino.destination(2),  // fd 2 = stderr
);
```

### 4.3 Tool Registration Pattern (`registerXxxTools`)

Each domain module exports one function that takes the `McpServer` and registers all tools in that domain. `index.ts` never sees individual tool definitions.

```typescript
// src/registry.ts
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { registerFsTools }   from './tools/fs.js';
import { registerGitTools }  from './tools/git.js';
import { registerExecTools } from './tools/exec.js';

export function registerAllTools(server: McpServer) {
  registerFsTools(server);
  registerGitTools(server);
  registerExecTools(server);
}
```

```typescript
// src/schemas/fs.ts
import { z } from 'zod';

// .strict() rejects unknown keys; .describe() populates JSON Schema for LLM
export const FsReadInput = z.object({
  path:   z.string().describe('Absolute path of the file to read.'),
  offset: z.number().int().min(0).default(0)
            .describe('Character offset to start reading (for pagination).'),
  limit:  z.number().int().min(1).default(25_000)
            .describe('Max characters to return per call.'),
}).strict();

export type FsReadInput = z.infer<typeof FsReadInput>;
```

```typescript
// src/tools/fs.ts
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { readFile }  from 'node:fs/promises';  // async: never block the event loop
import { FsReadInput } from '../schemas/fs.js';
import { truncate }    from '../utils/truncate.js';
import { log }         from '../services/logger.js';

export function registerFsTools(server: McpServer) {
  server.registerTool(
    'fs_read_file',
    {
      title:       'Read file',
      description: 'Read a UTF-8 text file with character-offset pagination.',
      inputSchema: FsReadInput.shape,  // pass .shape, not the schema object directly
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    async (args) => {
      // NEVER throw from a tool handler — always return isError: true
      try {
        const content = await readFile(args.path, 'utf8');
        const { text, has_more, next_offset, total } = truncate(
          content, args.offset, args.limit,
        );
        return {
          content: [{ type: 'text', text }],
          structuredContent: { has_more, next_offset, total },
        };
      } catch (err) {
        log.error({ err, path: args.path }, 'fs_read_file failed');  // stderr
        const msg = err instanceof Error ? err.message : String(err);
        return {
          content: [{ type: 'text', text: `Error reading ${args.path}: ${msg}` }],
          isError: true,
        };
      }
    },
  );
}
```

**Tool naming convention:** `snake_case`, domain-prefixed (e.g. `fs_read_file`, `git_commit`, `exec_run_command`). With 20+ tools across domains, the prefix prevents name collisions and makes the tool namespace self-documenting.

### 4.4 Error Handling (`isError` pattern)

> **Rule: a tool handler must never throw.** An uncaught exception bubbles up as a protocol-level JSON-RPC error. The Brain receives a hard error it cannot inspect, reason about, or retry. The conversation loop can crash.

Wrap every handler body in `try/catch`. On any error: log to stderr, return `{ content: [{ type: 'text', text: errorMessage }], isError: true }`. The LLM receives the error text as a normal tool result and can adapt.

```typescript
// Generic error-capture wrapper for handlers that call out to skills
async function safecall<T>(
  label: string,
  fn: () => Promise<T>,
): Promise<T | { content: [{ type: 'text'; text: string }]; isError: true }> {
  try {
    return await fn();
  } catch (err) {
    log.error({ err }, `${label} failed`);
    const msg = err instanceof Error ? err.message : String(err);
    return { content: [{ type: 'text', text: `${label}: ${msg}` }], isError: true };
  }
}
```

### 4.5 Output Truncation & Pagination

Any tool that reads files, git logs, directory trees, or command output must enforce `CHARACTER_LIMIT`. Returning 50k–100k characters silently blows the Brain's context window — it is a quality killer, not a crash.

```typescript
// src/constants.ts
export const CHARACTER_LIMIT = 25_000;
```

```typescript
// src/utils/truncate.ts
import { CHARACTER_LIMIT } from '../constants.js';

export interface TruncateResult {
  text:        string;
  has_more:    boolean;
  next_offset: number | null;
  total:       number;
}

export function truncate(
  text:   string,
  offset: number = 0,
  limit:  number = CHARACTER_LIMIT,
): TruncateResult {
  const slice    = text.slice(offset, offset + limit);
  const has_more = offset + limit < text.length;
  return {
    text: slice + (has_more
      ? `\n\n[TRUNCATED: showing chars ${offset}–${offset + slice.length} of ${text.length} total. ` +
        `Call again with offset=${offset + limit} to continue.]`
      : ''),
    has_more,
    next_offset: has_more ? offset + limit : null,
    total: text.length,
  };
}
```

The `structuredContent` field carries the pagination metadata for machine consumption; the `content[].text` suffix carries it for LLM consumption.

### 4.6 Graceful Shutdown

Containers (RunPod, k8s) send `SIGTERM` on scale-down. Without a handler the process is killed mid-flight: in-flight tool calls are lost, the Python client sees a broken pipe, and the Brain stalls on a hung `Future`.

The shutdown handler (shown in section 4.2) must:
1. Guard with a `shuttingDown` flag so re-entrant signals are a no-op.
2. Call `server.close()` to stop accepting new work.
3. Call `transport.close()` to close the stdio channel.
4. Call `process.exit(0)`.

Register both `SIGINT` (interactive) and `SIGTERM` (container orchestrators). Register `uncaughtException` as a last-resort flush.

---

## 5. Python MCP Client Implementation

### 5.1 `MCPClientManager` Class

`MCPClientManager` is the single object the Brain interacts with for all MCP tool calls. It owns the subprocess, the session, the reconnection supervisor, and the request multiplexer.

```python
# mcp_client_manager.py
from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.client.stdio import get_default_environment


class CircuitOpenError(Exception):
    """Raised when the circuit breaker has tripped after repeated server crashes."""


@dataclass
class _Req:
    name:    str
    args:    dict[str, Any]
    future:  asyncio.Future
    timeout: float = 30.0


class MCPClientManager:
    """
    Single owning task for the MCP session lifecycle.

    CRITICAL INVARIANT: stdio_client() and ClientSession() context managers
    are entered AND exited inside _run(), which runs in a single dedicated
    asyncio.Task. They are never entered or exited from a caller's task.
    This is the only correct way to satisfy anyio's cancel-scope rule.
    """

    def __init__(
        self,
        params: StdioServerParameters,
        *,
        max_failures: int  = 5,
        window:       float = 60.0,
        call_timeout: float = 30.0,
    ) -> None:
        self._params       = params
        self._inbox:       asyncio.Queue[_Req] = asyncio.Queue()
        self._ready        = asyncio.Event()
        self._task:        asyncio.Task | None = None
        self._failures:    deque[float] = deque()
        self._max_failures = max_failures
        self._window       = window
        self._call_timeout = call_timeout
        self.tools:        list = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the owning lifecycle task. Call once before any tool calls."""
        self._task = asyncio.create_task(self._run(), name="mcp-lifecycle")

    async def wait_ready(self, timeout: float = 10.0) -> None:
        """Block until the session is initialized and ready."""
        await asyncio.wait_for(self._ready.wait(), timeout=timeout)

    async def call_tool(
        self,
        name:    str,
        args:    dict[str, Any],
        timeout: float | None = None,
    ) -> Any:
        """
        Submit a tool call and await the result.
        Many coroutines can call this concurrently — they all multiplex
        through the single session via the inbox Queue.
        """
        fut = asyncio.get_running_loop().create_future()
        await self._inbox.put(_Req(name, args, fut, timeout or self._call_timeout))
        return await fut

    async def shutdown(self) -> None:
        """Cancel the owning task and wait for cleanup."""
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    # ------------------------------------------------------------------
    # Internal: owning lifecycle task
    # ------------------------------------------------------------------

    def _breaker_open(self) -> bool:
        now = time.monotonic()
        # Expire old failure timestamps
        while self._failures and now - self._failures[0] > self._window:
            self._failures.popleft()
        return len(self._failures) >= self._max_failures

    def _drain_with(self, exc: Exception) -> None:
        """Fail all pending requests with exc (used when circuit is open)."""
        while not self._inbox.empty():
            try:
                req = self._inbox.get_nowait()
                if not req.future.done():
                    req.future.set_exception(exc)
            except asyncio.QueueEmpty:
                break

    async def _run(self) -> None:
        """
        The single owning task.

        Both stdio_client() and ClientSession() are entered here and will
        exit here — in the SAME task. This satisfies anyio's cancel-scope
        invariant and prevents RuntimeError: "Attempted to exit cancel scope
        in a different task than it was entered in".
        """
        backoff = 0.5
        while True:
            if self._breaker_open():
                err = CircuitOpenError(
                    f"MCP server crashed {self._max_failures}+ times "
                    f"in {self._window}s; circuit open"
                )
                self._drain_with(err)
                await asyncio.sleep(self._window)
                self._failures.clear()
                continue

            try:
                async with stdio_client(self._params) as (read, write):      # enter
                    read = _robust_stdio_filter(read)  # drop non-JSON stdout noise
                    async with ClientSession(read, write) as session:         # enter (same task)
                        await session.initialize()
                        # Always re-list after every (re)connect — never use stale cache
                        result = await session.list_tools()
                        self.tools = result.tools
                        self._ready.set()
                        backoff = 0.5  # reset on successful connect
                        await self._serve(session)   # multiplex loop

            except asyncio.CancelledError:
                # Requested shutdown — exit cleanly; cancel scopes exit here (same task)
                return
            except Exception:
                self._failures.append(time.monotonic())
                self._ready.clear()
                jitter = random.uniform(0, backoff)
                await asyncio.sleep(backoff + jitter)
                backoff = min(backoff * 2, 30.0)
                # cancel scopes exit here, in the same task that entered them

    async def _serve(self, session: ClientSession) -> None:
        """
        Multiplex tool calls from the inbox Queue onto the session.
        Runs until the session dies (exception) or we are cancelled.
        """
        while True:
            req = await self._inbox.get()
            try:
                with anyio.fail_after(req.timeout):          # per-call timeout
                    result = await session.call_tool(req.name, req.args)
                if not req.future.done():
                    req.future.set_result(result)
            except TimeoutError as exc:
                if not req.future.done():
                    req.future.set_exception(exc)
            except Exception as exc:
                if not req.future.done():
                    req.future.set_exception(exc)
                raise  # bubble connection-level errors back to _run for reconnect
```

### 5.2 Single Owning Task Pattern (anyio cancel scope)

> **The dominant production error on the Python side is:**
> `RuntimeError: Attempted to exit cancel scope in a different task than it was entered in`

This error surfaces when code enters `stdio_client()` or `ClientSession()` in one `asyncio.Task` (e.g. the main task that calls `async with`) and then tries to close the `AsyncExitStack` from a different task (e.g. a shutdown handler, a timeout callback, or a background task).

anyio's cancel scopes are **strictly task-scoped**. The scope must be exited by the same task that entered it.

**The fix is architectural, not a patch:** the entire connection lifecycle — enter `stdio_client`, enter `ClientSession`, `initialize()`, `list_tools()`, `_serve()` loop, and all exits — must live inside a single, dedicated `asyncio.Task`. `MCPClientManager.start()` creates this task. Nothing else ever touches the session objects.

This is confirmed in python-sdk issues #79, #252, #521, #577.

```
WRONG (common mistake):
    async def setup(self):
        self._exit_stack = AsyncExitStack()
        read, write = await self._exit_stack.enter_async_context(stdio_client(...))
        self._session = await self._exit_stack.enter_async_context(ClientSession(...))
        # ^ entered in task A

    async def teardown(self):
        await self._exit_stack.aclose()
        # ^ exits in task B → RuntimeError

CORRECT:
    async def _run(self):  # owning task
        async with stdio_client(...) as (read, write):   # enter here
            async with ClientSession(read, write) as s:  # enter here
                await self._serve(s)
                # exits happen here automatically, in the same task
```

### 5.3 Request Multiplexer (`asyncio.Queue` + `Future`)

The Brain may dispatch many concurrent tool calls. All of them share the single `ClientSession`. The `asyncio.Queue[_Req]` decouples the callers from the session's serve loop.

```
Brain coroutine A  ──►  inbox.put(_Req("fs_read_file", ...))  ──►  await future_A
Brain coroutine B  ──►  inbox.put(_Req("git_commit",   ...))  ──►  await future_B
Brain coroutine C  ──►  inbox.put(_Req("exec_run",     ...))  ──►  await future_C

                       asyncio.Queue (FIFO)
                             │
                             ▼
                    _serve() loop (owning task)
                    ├─ dequeue _Req A → session.call_tool() → future_A.set_result()
                    ├─ dequeue _Req B → session.call_tool() → future_B.set_result()
                    └─ dequeue _Req C → session.call_tool() → future_C.set_result()
```

Note: the MCP `ClientSession` is itself not concurrent within a single call — it is effectively a request-response channel. The `_serve()` loop serializes dispatch. If true concurrency over one session is needed, investigate whether the MCP spec allows concurrent in-flight requests (it uses request IDs; some implementations do support pipelining).

### 5.4 Supervisor with Circuit Breaker

The supervisor lives inside `_run()`. It wraps the connection attempt in a `while True` loop with exponential backoff and jitter. The circuit breaker prevents a crash loop from consuming CPU/memory.

```python
# Circuit breaker state machine:
#
#  CLOSED (normal)
#    │ N failures within window seconds
#    ▼
#  OPEN (drain pending futures with CircuitOpenError)
#    │ sleep(window); clear failure log
#    ▼
#  HALF-OPEN (attempt one reconnect)
#    │ success → CLOSED
#    │ failure → OPEN
```

Parameters (constructor args):
- `max_failures`: number of crashes within `window` seconds that trip the breaker (default 5)
- `window`: sliding window in seconds (default 60.0)
- `backoff`: starts at 0.5s, doubles each failure, caps at 30.0s, reset on success

The backoff adds uniform jitter (`random.uniform(0, backoff)`) to prevent thundering-herd reconnects when the TS server restarts after a deploy.

### 5.5 Per-Call Timeout

Every tool call in `_serve()` is wrapped in `anyio.fail_after(req.timeout)`. If the TS handler hangs (e.g. skill process stalls), the timeout fires, cancels the call, raises `TimeoutError` into the caller's `Future`, and leaves the session intact for the next request.

```python
with anyio.fail_after(req.timeout):
    result = await session.call_tool(req.name, req.args)
```

If the timeout fires during a network-level hang (not just slow skill logic), the exception may propagate upward through `_serve()` and trigger a reconnect in `_run()`. This is intentional — a session that cannot complete a call within its timeout may be in a bad state.

The `_robust_stdio_filter` wrapper referenced in `_run()` is a defensive filter that discards non-JSON lines from the TS server's stdout before they reach the MCP parser. See section 7 for why this is necessary.

```python
# Minimal robust filter sketch
async def _robust_stdio_filter(reader):
    """
    Wraps the stdio read stream to silently drop lines that are not
    valid JSON-RPC. Prevents json.JSONDecodeError from crashing the session
    if the TS server emits non-JSON stdout (e.g. a stray console.log).
    """
    # Implementation wraps the reader's readline() to validate JSON
    # before passing to ClientSession. See Dicklesworthstone/ultimate_mcp_client
    # for a production reference.
    ...
```

---

## 6. BDD Test Scenarios

### TypeScript MCP Server

```gherkin
Feature: TypeScript MCP stdio server
  As the Labmate Python orchestrator (Brain)
  I want a robust TypeScript MCP server over stdio
  So that tool calls are discoverable, isolated, hygienic, and bounded

  Background:
    Given an McpServer is bootstrapped with StdioServerTransport
    And all domain tool modules have been registered via registerAllTools(server)

  Scenario: Tool discovery returns valid self-contained schemas
    Given the MCP server has started and completed the initialize handshake
    When the client sends a tools/list request
    Then the response includes every registered tool
    And each tool has a name, a description, and an inputSchema that is valid JSON Schema
    And no inputSchema contains an external $ref
    And every input field carries a description string

  Scenario: stdout hygiene is preserved under logging
    Given a tool handler that emits log output during execution
    When the handler emits log output
    Then the log bytes are written to stderr only (fd 2)
    And stdout contains exclusively well-formed JSON-RPC 2.0 messages
    And the client's JSON-RPC parser never encounters a framing error

  Scenario: Errors are isolated as tool-level results, not protocol errors
    Given a tool handler that throws an exception during execution
    When the tool is invoked via tools/call
    Then the handler's try/catch converts the throw into a CallToolResult
    And the result has isError set to true
    And the result content includes a text block with the error message
    And the client receives a normal tools/call response (not a JSON-RPC protocol error)

  Scenario: Large output is truncated and paginated
    Given a tool that would naturally return 100000 characters
    When the tool is invoked without offset
    Then the returned content text is truncated at CHARACTER_LIMIT (25000 chars)
    And the result includes a truncation notice with next_offset
    And the structuredContent sets has_more to true with a valid next_offset
    And re-invoking the tool with that offset returns the following page

  Scenario: Zod .strict() rejects unknown keys
    Given a tool registered with a .strict() Zod schema
    When the client sends a tools/call with an unexpected extra field
    Then the server returns an isError: true result
    And the error message describes the unknown field
    And the server does not crash or accept the malformed input

  Scenario: Graceful shutdown on container SIGTERM
    Given the server has an in-flight tool call executing
    When the process receives SIGTERM
    Then the server stops accepting new requests
    And the in-flight call is allowed to complete or is flushed
    And transport.close() and server.close() are invoked
    And the process exits with code 0 with no lost JSON-RPC responses

  Scenario: Event loop is never blocked by synchronous I/O
    Given 10 concurrent tool/call requests arrive
    When each handler uses async fs.promises APIs
    Then all 10 requests are processed concurrently
    And no request is stalled waiting for another handler's blocking I/O
```

### Python MCP Client

```gherkin
Feature: Long-lived persistent MCP session
  Scenario: Many sequential tool calls reuse one subprocess
    Given the MCP server subprocess is started and the ClientSession is initialized once
    When 100 sequential tool calls are made through the client manager
    Then the same subprocess (same PID) handles all 100 calls
    And no additional initialize() handshake or subprocess spawn occurs between calls

Feature: anyio cancel-scope task isolation
  Scenario: Session lifecycle is confined to the owning task
    Given the session was entered and is owned by lifecycle task A (_run)
    When a different coroutine calls shutdown() from task B
    Then the shutdown routes the cancel through the owning task A
    And no RuntimeError about cancel scopes in a different task is raised
    And the session exits cleanly

  Scenario: Direct cross-task session close is rejected at design time
    Given a naive implementation that exposes the AsyncExitStack to callers
    When a foreign task attempts to close the AsyncExitStack
    Then a RuntimeError "Attempted to exit cancel scope in a different task" is raised
    And this pattern is rejected by code review in favor of MCPClientManager

Feature: Circuit breaker on crash loop
  Scenario: Client stops reconnecting after repeated crashes
    Given the MCP server crashes 5 times within a 60-second window
    When the 6th crash occurs
    Then the supervisor opens the circuit and stops attempting reconnects
    And a CircuitOpenError is surfaced to all pending callers in the inbox
    And reconnection is only retried after the breaker cooldown elapses

  Scenario: Circuit resets after cooldown
    Given the circuit is open after 5 crashes
    When 60 seconds elapse (the window duration)
    Then the supervisor clears the failure log and attempts one reconnect
    And on a successful connect the circuit returns to closed state

Feature: Per-call timeout with cancellation
  Scenario: A hung tool handler is cancelled and reported
    Given a tool handler that never returns
    When the per-call anyio.fail_after timeout expires
    Then a TimeoutError is raised to the calling coroutine's Future
    And the session remains alive and usable for subsequent calls

Feature: Tool cache refresh on reconnect
  Scenario: Reconnect always re-lists tools
    Given the MCP server was restarted with a different tool set
    When the supervisor reconnects and calls initialize()
    Then list_tools() is called immediately after initialize()
    And manager.tools reflects the new tool set, not the stale cache
```

---

## 7. Common Pitfalls

### CRITICAL — Must address before any production use

---

#### [P1] STDOUT POLLUTION (TypeScript side — #1 stdio killer)

**What it is:** Any `console.log()`, `process.stdout.write()`, `dotenv` banner, ORM query log, or any third-party library that writes to stdout corrupts the JSON-RPC stream. The Python MCP parser encounters non-JSON bytes interleaved with valid JSON-RPC messages and throws `json.JSONDecodeError`, crashing the session.

**Why it is silent:** The server keeps running. Tool calls intermittently fail or hang with no clear error. There is no crash, no stack trace, and the failure mode looks like a network issue. This is the hardest-to-debug failure in the entire bridge.

**The fix:**
- Configure pino to write to `fd 2` (stderr). This is set in `src/services/logger.ts` and must never change.
- Audit every npm dependency at startup for stdout writes. Use `--inspect` or redirect stdout to a file and diff against `[]` in tests.
- In CI: pipe the server's stdout through a JSON-RPC validator that fails on non-JSON bytes.
- On the Python side: wrap the reader with `_robust_stdio_filter()` as a last-resort defense, but never rely on it as the primary fix.

**Rule:** `stdout` is JSON-RPC ONLY. `stderr` is for everything else. There are no exceptions.

---

#### [P2] anyio CANCEL-SCOPE VIOLATION (Python side — dominant production error)

**What it is:** `RuntimeError: Attempted to exit cancel scope in a different task than it was entered in`

**When it fires:** Whenever code enters `stdio_client()` or `ClientSession()` context managers in one `asyncio.Task` and later closes/exits them from a different task. This includes:
- Storing an `AsyncExitStack` on `self` and calling `aclose()` from a shutdown handler running in a different task
- Calling `async with ClientSession(...)` in a "setup" method and exiting it in a "teardown" method called from a different coroutine

**Why it happens:** anyio implements cancel scopes as task-local stack frames. The scope is pushed onto the current task's stack on entry. Trying to pop it from a different task's stack is undefined behavior and anyio raises immediately.

**The fix:** All of `stdio_client()`, `ClientSession()`, `initialize()`, the serve loop, and all exits live inside `_run()`, which is the single owning `asyncio.Task`. Callers never hold references to the session or exit stack. See section 5.2.

**Confirmed in:** python-sdk issues #79, #252, #521, #577.

---

### Other pitfalls

| # | Side | Pitfall | Fix |
|---|---|---|---|
| P3 | TS | Throwing from a tool handler | Wrap every handler in `try/catch`; return `isError: true` |
| P4 | TS | Zod schema without `.strict()` | Use `.strict()` on every `z.object()` |
| P5 | TS | Missing `.describe()` on Zod fields | Every field and tool must carry a description string |
| P6 | TS | External `$ref` in JSON Schema | Keep schemas inline; bundle `$defs` within the same schema doc |
| P7 | TS | No output truncation | Enforce `CHARACTER_LIMIT = 25_000`; add pagination metadata |
| P8 | TS | Missing `SIGTERM` handler | Register both `SIGINT` and `SIGTERM` in `index.ts` |
| P9 | TS | Monolithic `index.ts` | Domain modules + `registerXxxTools`; never inline tools in `index.ts` |
| P10 | TS | Blocking the event loop in a handler | Use `fs.promises`, `exec`/`spawn` with `await`; offload CPU work to worker threads |
| P11 | TS | Not pinning the SDK version | Pin exact version in `package.json`; test before bumping |
| P12 | Py | Per-call reconnect | One persistent `ClientSession`; reconnect only on crash |
| P13 | Py | No circuit breaker | Sliding-window breaker with `CircuitOpenError` drain |
| P14 | Py | Blocking `call_tool` without timeout | Wrap every call in `anyio.fail_after(timeout)` |
| P15 | Py | External stdout reader on subprocess | SDK must be the sole reader of the subprocess stdout |
| P16 | Py | Missing SIGTERM/SIGKILL escalation | Spec-compliant shutdown: close stdin → wait → SIGTERM → SIGKILL |
| P17 | Py | Stale tool cache after reconnect | Always call `list_tools()` after every `initialize()` |
| P18 | Py | Assuming streaming over stdio | stdio returns one whole `CallToolResult`; use `notifications/progress` for incremental status |

---

## 8. Dependencies

### TypeScript (MCP Server)

| Package | Type | Version | Purpose |
|---|---|---|---|
| `@modelcontextprotocol/sdk` | runtime | pin exact (e.g. `1.x.y`) | MCP server, transport, protocol |
| `zod` | runtime | `^3.22` | Input schema validation with `.strict().describe()` |
| `pino` | runtime | `^9` | Structured logging to stderr (fd 2) |
| `typescript` | devDep | `^5.4` | TS compiler |
| `tsx` | devDep | `^4.19` | Run/watch TS in dev without build step |
| `@types/node` | devDep | `^22` | Node type definitions |

`tsconfig.json` must set: `"strict": true`, `"target": "ES2022"`, `"module": "Node16"` (or `NodeNext`), `"moduleResolution": "Node16"`. `package.json` must set `"type": "module"`.

### Python (MCP Client)

| Package | Version constraint | Purpose |
|---|---|---|
| `mcp` | `>=1.27,<2` | MCP Python SDK: `ClientSession`, `stdio_client`, `StdioServerParameters`. Pin below v2 — v2 beta targets 2026-06-30 with breaking API changes |
| `anyio` | `>=4.9` | Cancel-scope primitives; `fail_after` for per-call timeouts |
| `pydantic` | `>=2` | MCP message model validation (SDK uses pydantic v2) |
| `backoff` or `tenacity` | `backoff>=2.2` / `tenacity>=8` | Declarative exponential backoff + jitter for the supervisor reconnect loop (choose one) |

Runtime Python version: `>=3.11` (for `asyncio.Task` type hints and `TaskGroup`).

---

## 9. Reference Papers & Repos

### Papers

| Title | Reference | Relevance |
|---|---|---|
| Model Context Protocol Specification | Anthropic, spec version 2025-11-25, https://modelcontextprotocol.io/specification/2025-11-25 | Canonical normative reference: JSON-RPC 2.0 lifecycle, capability negotiation, tools/resources/prompts |
| A Survey of Agent Interoperability Protocols: MCP, ACP, A2A, ANP | Ehtesham et al. 2025 — arxiv:2505.02279 | Survey covering MCP architecture and transport trade-offs |
| MCP: Landscape, Security Threats, and Future Research Directions | Hou et al. 2025 — Preprints 202504.0245 | Architecture, transport, and lifecycle patterns; security considerations |
| A Survey of the Model Context Protocol (MCP) | 2025 — arxiv:2504.16736 | Client/server session management and stdio vs HTTP transport trade-offs |
| MCP for Agentic Systems: Architecture, Security, and Tooling | 2025 — arxiv:2505.06416 | Production deployment concerns: supervision, reconnection |

### Repos

| Repo | URL | Relevance |
|---|---|---|
| `modelcontextprotocol/typescript-sdk` | https://github.com/modelcontextprotocol/typescript-sdk | Official TS SDK; canonical `McpServer`, `StdioServerTransport`, `registerTool` |
| `modelcontextprotocol/python-sdk` | https://github.com/modelcontextprotocol/python-sdk | Official Python SDK; source of anyio cancel-scope constraint (issues #79, #252, #521, #577) |
| `anthropics/skills` (`mcp-builder/reference/node_mcp_server.md`) | https://github.com/anthropics/skills/blob/main/skills/mcp-builder/reference/node_mcp_server.md | Anthropic's own production reference guide for Node/TS MCP servers |
| `cyanheads/mcp-ts-template` | https://github.com/cyanheads/mcp-ts-template | Production-grade TS template: domain-driven, pluggable auth, OpenTelemetry, stdio + HTTP |
| `yigitkonur/example-mcp-server-stdio` | https://github.com/yigitkonur/example-mcp-server-stdio | Definitive educational stdio reference: layered architecture, stdout vs stderr distinction |
| `cyanheads/git-mcp-server` | https://github.com/cyanheads/git-mcp-server | Real-world 28-tool example: best model for Labmate's 20+ tool registry |
| `QuantGeekDev/mcp-framework` | https://github.com/QuantGeekDev/mcp-framework | Batteries-included TS framework with automatic directory-based tool discovery |
| `aashari/boilerplate-mcp-server` | https://github.com/aashari/boilerplate-mcp-server | Layered boilerplate with Zod v4, dual transport with fallback, TOON output format |
| `Dicklesworthstone/ultimate_mcp_client` | https://github.com/Dicklesworthstone/ultimate_mcp_client | `RobustStdioSession` defensive stdout parser + circuit-breaker reference |
| `NousResearch/hermes-agent` | https://github.com/NousResearch/hermes-agent | Long-lived single-session orchestration owned by one task |
| `langchain-ai/langchain-mcp-adapters` | https://github.com/langchain-ai/langchain-mcp-adapters | Multi-server client management and tool namespace dispatch |
| `openai/openai-agents-python` | https://github.com/openai/openai-agents-python | `MCPServerStdio`/`MCPServerStreamableHttp` wrappers and session reuse patterns |

---

## 10. SOTA Improvements

These are improvements to consider after the initial stdio bridge is stable.

### Streamable HTTP Transport (spec 2025-03-26, SDK 1.10.0+)

The modern remote transport. Single `/mcp` endpoint handling `POST`/`GET`/`DELETE`, with server-to-client SSE for true intra-call streaming (impossible over stdio). Stateful or stateless modes. Optional resumability via an `EventStore` and priming events.

**Recommendation for Labmate:** Keep stdio for the local Brain-to-server link (lowest latency, no network surface). Expose remote skills over `StreamableHTTPServerTransport` when skills run on separate machines or containers. Streamable HTTP is now the cross-host baseline for Claude, ChatGPT, VS Code, and Goose.

### Tasks Primitive (experimental, Nov 2025 spec, requires Streamable HTTP)

A long-running tool invocation returns a **handle** instead of a result. The client polls for completion. Ideal for slow skills (full builds, large repo scans) so a tool call never blocks the Brain or times out. Supersedes ad-hoc job-ID polling patterns.

### MCP Proxy / Gateway

An MCP proxy (e.g. `mcp-proxy`, IBM `mcp-context-forge`) sits in front of multiple TS skill servers and acts as a single ingress. The Brain talks to one endpoint instead of managing N subprocesses. Enables load balancing and isolates a crashed skill domain from the rest.

### Connection Pooling Across Skill Domains

One `MCPClientManager` per skill domain (fs, git, exec, …), each in its own subprocess, dispatched by tool namespace prefix. A slow or crashed skill server is isolated — the git tools don't go down when the exec tool server hangs.

### mcp v2 Migration Planning

Pin `>=1.27,<2` now. v2 beta targets 2026-06-30, stable 2026-07-27. v2 brings breaking API changes to `ClientSession` and the transport layer. Track the migration guide and schedule a dedicated upgrade sprint before stable release.

### OpenTelemetry Observability

Add `@opentelemetry/sdk-node` to the TS server with spans per tool call. Export traces to a local Jaeger or OTLP collector. This is the only way to diagnose latency distributions across 20+ tools at scale. The `cyanheads/mcp-ts-template` repo has a working reference.

### TOON Output Format (Token-Optimized Output Notation)

The `aashari/boilerplate-mcp-server` introduces TOON, a compact structured output format for tool results that reduces token consumption by the Brain. Consider adopting for large-output tools (git log, directory trees) after initial integration is stable.
