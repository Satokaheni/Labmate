# Local-Harness Seam Map (strangler insertion points)

> Companion to `2026-07-03-local-harness-rearchitecture-design.md`. Produced in
> Piece 0. Pins the exact `file:line` where each later piece adds its
> `if local_mode_enabled():` branch. Line numbers are anchors as of the Piece 0
> commit — re-confirm by symbol if they drift.

The flag reader is `services/orchestrator/local_mode.py::local_mode_enabled()`
(default OFF = pod mode). Piece 0 only reads + logs it; no branch yet.

## Seam 1 — LangGraph checkpointer (Piece 1)
- **Insertion point:** `services/orchestrator/graph.py` `build_graph()`, the
  `MongoDBSaver` construction (~lines 982, 1015–1019: `from
  langgraph.checkpoint.mongodb import MongoDBSaver`; `client =
  MongoClient(mongo_uri)`; `cp = MongoDBSaver(client, db_name=db_name)`).
- **Local impl:** `SqliteSaver` from `langgraph-checkpoint-sqlite`, file path
  under the per-user local state dir. Branch on the flag; keep returning
  `(graph, checkpointer)`.
- **Caller to keep intact:** `main.py` `run()` (~line 352) `graph, _cp =
  build_graph(...)`.

## Seam 2 — Sessions / turns / workspaces state (Piece 2, folds in fix-A continuity)
- **Insertion point:** `main.py` `run()` (~line 243) `async with
  StorageManager() as _sm:`, plus `StorageManager.__init__`
  (`storage_manager.py:57–74`) which reads `MONGO_URI/CHROMA_URL/REDIS_URL`.
- **Public interface to preserve** (what the brain calls): `context_manager`,
  `consolidator`, `workspaces`, `loop_checkpoint_collection`,
  `store_episode`, `store_memory`, `close_memory`,
  `boost_memory_importance`, `decay_expired_memories`, `search_memories`,
  `search_turns`, `cache_set`, `cache_get`, `enqueue_task`, and the
  `__aenter__/__aexit__` context-manager contract.
- **Local impl:** SQLite-backed `StorageManager` (sessions, chat_turns,
  workspaces, checkpoints, telemetry). Continuity (`search_turns` /
  `context_manager` reads) becomes a local DB read.
- **Test seam already present:** `StorageManager.from_clients()`
  (`storage_manager.py:76`) + `tests/services/orchestrator/conftest.py`
  `storage` fixture.

## Seam 3 — Chroma / semantic search (Piece 3)
- **Insertion point:** `storage_manager.py::_get_chroma()` (lines 158–161,
  lazy `chromadb.AsyncHttpClient`) feeding `search_memories()` (line 288).
  Consumers: `memory_search.py` (`MemorySearch`), `session_search.py`
  (`SessionSearch`), and the pod codegraph embedder
  (`main.py:265` `pod_codegraph_enabled()`).
- **Local impl:** drop Chroma; `search_memories`/`memory_search` become an
  agentic grep/read + `AGENTS.md` path, or a no-op behind the flag. Optional
  future SQLite-FTS. `session_search` already uses Mongo `$text` → moves with
  Seam 2.

## Seam 4 — Redis (goals / events / steer / cancel / tool-results) (Piece 4, largest)
- **Goals consume:** `main.py` `_loop()` (lines 402–430) `xreadgroup` on
  `labmate:goals` + `xack` (932); `_ensure_group()` (939). Local: goals become
  a direct in-process call into `_handle()` (no stream, no consumer group).
- **Results:** `main.py::_write_result()` (934–937) `SET labmate:result:` +
  `PUBLISH`. Local: direct return / in-proc future.
- **Events:** `events.py::EventEmitter.emit()` (108–126) `xadd
  labmate:events:`. Local: in-process emitter feeding the local gateway.
- **Cancel/steer:** `events.py` `is_cancelled` (139, `EXISTS`), `write_steer`
  (151, `SET EX`), `read_and_clear_steer` (162, `GETDEL`). Local: in-process
  flags/queue keyed by task_id.
- **Cache:** `storage_manager.py` `cache_set/cache_get` (381–388). Local:
  SQLite or in-memory TTL dict.

## Seam 5 — Tool dispatch / delegation (Piece 5, folds in fix-B routing, fixes file-read)
- **Insertion point:** `coding_orchestrator.py` `_run_react_loop` tool dispatch
  — `request_local_tool(...)` calls at lines ~1198 (`run_tests`), ~1281
  (`read_file`/`write_file`/`list_dir`), ~1290 (write-verify read-back). The
  delegation mechanism is `local_tools.py::request_local_tool` (101–155):
  emit `tool.request` → block on `labmate:tool-results:` XREAD.
- **Manifest/threading to drop in local mode:** `tool_manifest.py`
  (`parse_manifest`, `manifest_local_tool_names`), `client_context.py`
  (`current_manifest`, `current_workspace_root` ContextVars), set in
  `main.py:599–605`.
- **Local impl:** `read_file`/`write_file`/`list_dir`/`run_tests` run directly
  on the local FS/shell — no delegation, no manifest, no workspace-root
  threading. **Fixes the file-read mis-route.** fix-B: broaden
  `edit_intent.requires_editing` routing so file-access tasks also enter
  `_run_react_loop` (so the local file tools are reached).

## Seam 6 — Gateway + entry point (Piece 6)
- **Insertion point:** `main.py` `OrchestratorProcess.run()` boot sequence
  (239–396) + the client transports: `ws_gateway/server.py` (419–426 xadd
  goals), `ws_gateway/redis_bridge.py`, `cli/redis_client.py` (33 xadd),
  `cli/ws_client.py`. Local: gateway binds localhost, co-located with the
  in-process orchestrator; frontend points at `ws://localhost`.

## Seam 7 — Packaging / default flip (Piece 7)
- Install script; flip `local_mode_enabled()` default to ON; remove the
  pod/Redis/Mongo/Chroma/delegation paths once local mode is validated.
