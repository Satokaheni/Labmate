# Local Runtime — Redis removal + gateway co-location (Piece 4)

> Part of the pod→local rewrite on `experimental` (local-mode only; `main` keeps the pod version).
> Merges the spec's Piece 4 (Redis → in-process) and Piece 6 (localhost gateway) into one
> "local runtime" piece, per the user's decision, so the stack is always runnable end-to-end.

## Goal
Turn Labmate from a multi-process, Redis-brokered system into a **single Redis-free localhost
process**. After this piece the only remote dependency is the model (`GEMMA_BASE`).

## Before → After
| Concern | Pod (before) | Local runtime (after) |
|---|---|---|
| Goals | `XADD labmate:goals` → `XREADGROUP` consumer daemon | `OrchestratorProcess.submit_goal(payload)` → `asyncio.Queue` drained by `_loop` |
| Results | `SET/PUBLISH labmate:result` + pubsub/poll | `ResultRegistry.set_result` / `await wait_result` (per-task future) |
| Events | `XADD labmate:events:<id>` → gateway `XREAD` tail | `EventBus.publish("events:<id>")` → gateway `bus.subscribe(...)` |
| Steer/cancel | `SET labmate:steer/cancel` keys | `SignalRegistry` (in-proc) |
| Tool-results | `XADD/XREAD labmate:tool-results:<id>` | `EventBus` topic `tool-results:<id>` |
| Compaction state | Redis KV `core:/summary:/anchor:/summarized_through:` | SQLite `LocalStore.session_kv` (real cross-restart state) |
| Skills | `XADD labmate:skill-tasks` → separate `SkillWorker` daemon | in-process direct dispatch via a shared `SkillRegistry` |
| Gateway | separate FastAPI process proxying Redis ↔ WebSocket | co-located; imports the orchestrator, shares its in-proc runtime |

## Core: `services/orchestrator/inproc_bus.py`
Three pure-asyncio primitives (single event loop), constructed once in `OrchestratorProcess.__init__`:
- **`EventBus`** — per-topic fan-out pub/sub. `publish` is fire-and-forget (never raises); `subscribe`
  is **post-subscribe-only** (no replay) and drains buffered frames on `close()` (so the `turn.done`
  tail is never dropped). Callers therefore **subscribe before the triggering emit/submit**.
- **`SignalRegistry`** — per-task steer (consume-once) + cancel flag.
- **`ResultRegistry`** — per-task result future, tolerant of set-before-wait and wait-before-set.

## The runtime object
`OrchestratorProcess` **is** the runtime: `bus`, `signals`, `results`, `_goal_queue`, and
`submit_goal` are all set in `__init__`, so it's usable as a `runtime` immediately — no `run()`
split needed. `run()` still owns the heavy lifecycle (StorageManager, MCP subprocess, graph,
the shared `SkillRegistry` built at boot, background compactor/curator) and drains the goal queue.

## Single-process entrypoint: `services/local/main.py`
```
proc = OrchestratorProcess()                    # bus/signals/results ready
app  = build_app(Config.from_env(), runtime=proc)
server = uvicorn.Server(uvicorn.Config(app, host=127.0.0.1, port=8787, loop="none"))
await asyncio.gather-ish(server.serve(), proc.run())   # ONE asyncio loop
```
`loop="none"` makes uvicorn use the already-running loop, so the gateway and orchestrator share the
**same** bus/signals/results instances. Goals submitted before `run()`'s loop is ready simply wait
in the queue.

## Gateway co-location (`services/ws_gateway/server.py`)
A transport swap only — `translate_event`, the `_relay_task` accumulation, auth, and session
handling are unchanged. `_handle_send` **subscribes `events:<id>` before `submit_goal`**; `_relay_task`
iterates that subscription; `tool.result`→`local_tools.write_tool_result(bus)`, cancel→`signals.request_cancel`,
steer→`signals.write_steer`, compact→`submit_goal`+`results.wait_result`.

### Local tool.request fulfillment
The co-located gateway is the local client (same machine, FS access). When the orchestrator emits a
`tool.request` for a local FS tool the client didn't host, the relay executes it via
`services/cli/local_tool_executor.execute_local_tool` and posts the result on the bus — closing the
loop with `request_local_tool`. *(Piece 5 later replaces this round-trip with direct in-orchestrator
execution and fixes the file-read routing.)*

## Notable
- `AsyncOrchestrator.redis` was a truthy **sentinel** ("a local-tool client is attached"), not Redis
  I/O — renamed `local_client` (pure rename). Tool advertising is driven by the client manifest,
  independent of the sentinel.
- Deleted: `cli/redis_client.py`, `cli/redis_event_stream.py`, `skill_worker/worker.py`,
  `ws_gateway/redis_bridge.py` (its Redis-free `translate_event` relocated to `ws_gateway/event_translate.py`),
  and `redis`/`fakeredis` from all requirements.

## Testing
- Full `tests/services/ + services/memory/` suite: **1901 passed, 0 failures**.
- Offline integration test `tests/services/local/test_local_runtime.py` (fake runtime + FastAPI
  `TestClient` websocket) proves the co-located loop — relay/translate, steer/cancel, and a **real**
  `request_local_tool` write/read round-trip closed by the co-located fulfiller — with **no GPU and no
  Redis**. Live e2e against the GPU is deferred (box powered off).
