# Local State Unification (Mongo → SQLite) + Infra/Docs Local-ization — Design

**Date:** 2026-07-04
**Branch target:** `experimental` (pod version on `main` untouched)
**Piece:** "Memory piece" (state) + infra/docs local-ization, sequenced **before** Piece 7 packaging polish (per user decisions 2026-07-04).
**Execution:** subagent-driven-development (haiku implement → opus review).

## Goal

Drop **MongoDB** as a boot dependency of the local single-process harness
(`services/local/main.py`) so the only remote dependency is the model endpoint
(`GEMMA_BASE`). All persistent state moves to the existing SQLite `LocalStore`,
which becomes the **single source of truth** read by both the orchestrator and
the ws_gateway. **In the same piece**, bring the infrastructure scripts and
documentation into line with the local single-process runtime — they currently
lag the code (they launch the pre-Piece-4 split topology and provision three
services the runtime no longer uses).

## Non-Goals / Out of Scope

- **Markdown long-term memory** (`remember`/`recall`, MEMORY.md tooling) — still
  deferred to **post-Piece-7** (per `project_local_memory_model`). This piece is
  STATE only (sessions, auth users, turns, loop checkpoints).
- **Piece 7 packaging polish** — a real install *experience* for 2-3 users
  (guided `GEMMA_BASE` configuration, one-command bootstrap, cross-platform Mac
  paths) remains its own piece. THIS piece only removes the dead
  Mongo/Redis/Chroma provisioning + fixes the launch entrypoint + updates the
  core docs so nothing lags the code; it does not rewrite the install UX.
- **Cross-device session sync** — explicitly NOT built. Labmate is local-only
  ("runs entirely on your own machine"). Cross-machine pickup is handled at the
  memory layer by **git-tracked AGENTS.md + MEMORY.md**, not a sync server. (The
  claude.ai cross-device model is cloud-backed; the relevant references —
  Claude Code, hermes, openclaw — are all local single-source-of-truth.)
- **Data migration** from existing Mongo databases — none. Prod GPU is off, this
  is the experimental local rewrite, installs are fresh. New SQLite DB from empty.
- **SQLite-FTS5** for search — the eventual "option 3"; `LocalStore.search_turns`
  already does substring/regex search, which is sufficient here.

## Reference Alignment (why unify)

All three local references keep ONE local store as the single source of truth,
read by both the front-end/gateway and the agent:

- **hermes** — one `state.db` per profile; a single `SessionStore` the gateway
  writes and the agent's `session_search_tool` reads (FTS5 over the same db).
- **Claude Code** — one JSONL transcript per session *is* the session; UI + agent
  read the same file.
- **openclaw** — one SQLite `state.db`; its separate tables are per *subsystem*
  (workboard/openprose/auth), not two copies of the same session.

So the gateway's session store is unified onto `LocalStore` rather than kept as a
separate UI-only store. The frontend session list then shows exactly what the
orchestrator persisted.

## Current State (established by survey)

`LocalStore` (`services/orchestrator/local_store.py`, aiosqlite, process-wide
singleton via `get_local_store()`) ALREADY backs sessions, turns, continuity,
workspaces, and search, and already has tables: `chat_turns`, `sessions`,
`workspaces`, `users` (workspace-owner identity — NOT auth), `session_kv`.

Remaining Mongo touchpoints (all removed by this piece):
| Touchpoint | Disposition |
|---|---|
| `ws_gateway/user_store.py::MongoUserStore` | **delete**; add `SqliteUserStore` |
| `ws_gateway/mongo_session_store.py::MongoSessionStore` | **delete**; add `SqliteSessionStore` |
| `ws_gateway/server.py::_default_session_store` (Mongo probe) + default `MongoUserStore` | flip defaults to SQLite; drop the pymongo ping |
| `orchestrator/loop_checkpoint.py::CheckpointStore` (Motor collection) | back with a SQLite table |
| `orchestrator/storage_manager.py` (`AsyncIOMotorClient`, `ensure_indexes`, `loop_checkpoint_collection`) | strip Mongo; pure LocalStore facade |
| `orchestrator/db_indexes.py` | **delete** (Mongo index setup) |
| `memory/context_manager.py` `mongo_db` param (`self.db`, assigned never read) | **delete** the dead param |
| `motor` / `pymongo` in requirements | **remove** |

Interface seams that make this clean (KEEP unchanged):
- `ws_gateway/user_store.py::UserStore` Protocol (`find_by_email`/`create`/`count`) + `InMemoryUserStore`.
- `ws_gateway/sessions.py::InMemorySessionStore` interface (`create`/`list`/`get`/`rename`/`delete`/`turns`/`add_turn`/`set_debug`/`get_debug`) + `build_sessions_router`.
- `build_app(..., session_store=, user_store=)` overrides (tests already inject InMemory).

## Components

### 1. LocalStore additions (`services/orchestrator/local_store.py`)

**New table `auth_users`** (distinct from the workspace-owner `users` table):
```sql
CREATE TABLE IF NOT EXISTS auth_users (
    id            TEXT PRIMARY KEY,     -- "u-" + 12 hex
    email         TEXT UNIQUE NOT NULL, -- stored lowercased
    display_name  TEXT,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL,        -- "admin" | "user"
    created_at    TEXT NOT NULL
);
```
Methods: `auth_user_find_by_email(email) -> dict|None`,
`auth_user_create(*, id, email, display_name, password_hash, role, created_at) -> None`,
`auth_user_count() -> int`.

**New table `loop_checkpoints`**:
```sql
CREATE TABLE IF NOT EXISTS loop_checkpoints (
    task_id    TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,   -- JSON snapshot
    updated_at TEXT NOT NULL
);
```
Methods: `checkpoint_put(task_id, payload: dict) -> None` (upsert),
`checkpoint_get(task_id) -> dict|None`, `checkpoint_delete(task_id) -> None`.

**New method** `delete_session(session_id) -> None` — deletes the `sessions` row,
its `chat_turns`, and its `session_kv` entries (title/debug), in one transaction.

`session_kv` (existing) holds gateway per-session metadata: namespace `"gw"` with
keys stored as JSON `{"title":..., "debug":...}` per session_id (or two
namespaces `gw_title`/`gw_debug` — implementer's choice, kept internal to
`SqliteSessionStore`).

### 2. `SqliteUserStore` (`services/ws_gateway/user_store.py`)

Implements the `UserStore` Protocol, delegating to `LocalStore.auth_user_*`.
Returns `UserDoc` (`id/email/displayName/passwordHash/role/createdAt`). `create`
generates `id = "u-"+uuid4().hex[:12]` and `createdAt` ISO like `MongoUserStore`,
lowercases email. Constructed with a `LocalStore` (or `get_local_store()`).

### 3. `SqliteSessionStore` (`services/ws_gateway/sqlite_session_store.py`, new file)

Implements the session-store interface, delegating to `LocalStore`:
| Method | LocalStore backing |
|---|---|
| `create(...) -> dict` | `record_session(session_id, user_id=LOCAL_USER, task_preview=title)` + store title in `session_kv` |
| `list() -> list[dict]` | `list_sessions(LOCAL_USER, limit=...)` → map to gateway dict + title/debug from `session_kv` |
| `get(sid) -> dict\|None` | session row + `session_kv` title/debug |
| `rename(sid, title) -> dict\|None` | `session_kv` set title |
| `delete(sid) -> bool` | `delete_session(sid)` |
| `turns(sid) -> list[dict]` | `all_turns(sid)` → map `{seq,role,text}` to the gateway turn shape |
| `add_turn(sid, turn) -> None` | `append_turn(sid, role, text, reasoning=, tool_calls=)` |
| `set_debug/get_debug` | `session_kv` debug flag |

`LOCAL_USER` = a fixed local account id (the seeded admin) — the local harness is
single-user, so gateway `list()` (unscoped) maps to `list_sessions(LOCAL_USER)`.
The exact turn-dict shape returned by `turns()`/consumed by `add_turn()` is
copied verbatim from `InMemorySessionStore` so `build_sessions_router` +
`_relay_task` persistence are unchanged.

### 4. CheckpointStore → SQLite (`services/orchestrator/loop_checkpoint.py`)

`CheckpointStore.__init__` takes a `LocalStore` (not a Motor collection); its
three methods call `checkpoint_put`/`checkpoint_get`/`checkpoint_delete`. Still
best-effort (swallow+log). Default-OFF flag (`ENABLE_LOOP_CHECKPOINT=0`)
unchanged. `StorageManager.loop_checkpoint_collection` removed; the wiring in
`orchestrator/main.py` passes the LocalStore instead.

### 5. StorageManager (`services/orchestrator/storage_manager.py`)

Remove `AsyncIOMotorClient`, `self._mongo`/`self._db`, `ensure_indexes`,
`from_clients(mongo=...)`, `loop_checkpoint_collection`. `__aenter__` becomes a
no-op (or opens the LocalStore); `__aexit__` closes nothing Mongo. `context_manager`
drops `mongo_db=` (see §6). It stays a thin facade over `LocalStore`
(`local_store`, `workspaces`, `context_manager`, `search_turns`) so
`orchestrator/main.py`'s `async with StorageManager()` and consumers are
behavior-preserved.

### 6. ContextManager (`services/memory/context_manager.py`)

Drop the `mongo_db` constructor param and `self.db` (assigned, never read).
Update the one construction site in `StorageManager.context_manager`.

### 7. Deletions & requirements

Delete `ws_gateway/mongo_session_store.py`, `MongoUserStore`,
`orchestrator/db_indexes.py`. Remove `motor` and `pymongo` from
`requirements.txt` (and any service-local requirements). `_default_session_store`
returns `SqliteSessionStore` (no pymongo ping / no InMemory-on-failure fallback
for production; InMemory remains a test-injected override). `build_app`'s default
`user_store` becomes `SqliteUserStore`.

## Data Flow / Boot

`services/local/main.py` → `OrchestratorProcess.run()` no longer requires a live
Mongo: `async with StorageManager()` opens only SQLite. The gateway's
`build_app` constructs `SqliteUserStore` + `SqliteSessionStore` (both over the
same `LocalStore` DB file). Admin seeding (`if await user_store.count() == 0`)
works unchanged against SQLite. One DB file holds auth users, sessions,
chat_turns, workspaces, session_kv, loop_checkpoints.

## Infrastructure & Documentation (local-ization)

The `infrastructure/local/` scripts and top-level docs lag the code: they launch
the **pre-Piece-4 split topology** (`services.orchestrator.main` +
`services.ws_gateway.server` as two processes — which breaks the Piece-4
co-location that requires ONE process sharing the in-proc bus) and provision
**mongod + Redis + Chroma**, none of which the runtime uses (Chroma removed
Piece 3, Redis Piece 4, Mongo this piece). Bring them in line — in this piece,
so infra never lags the code:

### 8. `infrastructure/local/start.sh`
- **Launch the single process:** replace the separate orchestrator + ws_gateway
  `nohup python -m ...` blocks with one `python -m services.local.main` (writes
  one `local.pid`; readiness = gateway `/healthz` OK + the orchestrator "ready"
  log line). Keep the MCP-bridge `npm run build` step and the `serve-model.sh`
  guidance.
- **Remove** the mongod (replSet rs0), Redis, and Chroma start blocks + their
  wait/health loops + the `$DATA/{mongo,redis,chroma}` dir creation.

### 9. `infrastructure/local/stop.sh` + `status.sh`
- Stop/status the single `local.pid` process (+ the model server), not the old
  5 targets (mongod/redis/chroma/orchestrator/ws_gateway). Remove the
  Mongo/Redis/Chroma stop + health checks.

### 10. `infrastructure/local/install.sh`
- Remove MongoDB / Redis / Chroma installation + replica-set init steps. Keep
  Python deps, the MCP-bridge/node build, model-serving prerequisites, and add
  **ripgrep** (`rg`) as a recommended dep (the `search_files` tool prefers it;
  Python fallback exists) — record it in `docs/local-execution-prerequisites.md`.

### 11. `infrastructure/local/local.env`
- Remove `MONGO_URI`, `MONGO_URL`, `CHROMA_HOST/PORT/URL`, `REDIS_URL`. Keep
  `GEMMA_BASE`/`QWEN_BASE`, `WORKSPACE_PATH`, `LABMATE_GATEWAY_URL`,
  `CORS_ORIGINS`, `LOCAL_HOST`/`LOCAL_PORT`, model/tokenizer paths. Note the
  header comment that Mongo/Redis/Chroma are gone (single-process SQLite).

### 12. Docs — `infrastructure/local/{README.md,INSTALL.md}` + top-level `README.md` + `CLAUDE.md`
- Rewrite the architecture/requirements/service-URL sections to the **local
  single-process SQLite** topology: drop MongoDB/Redis/Chroma from the required
  services, drop the `lm-<name>` Docker container language and RunPod-required
  framing, describe `services.local.main` as the one process + `serve-model.sh`.
- `CLAUDE.md`: update the Architecture Map, Service URLs, and the "Memory /
  queues: MongoDB/Chroma/Redis" block to reflect SQLite-only local state
  (targeted edits — the harness-robustness/agentic-fix-loop sections stay).
- **Stale-comment sweep (Mongo/Redis/Chroma scope):** update the deferred prose
  refs noted in Piece 5 5d (`eval/seq_ab/local_tool_responder.py`,
  `test_tool_manifest.py:707`) and any code comments that still describe Mongo as
  the store. Broad packaging-doc polish stays Piece 7.

> Live E2E of the scripts (actually starting the process) needs the model server,
> which is powered off — so script changes are verified by **shellcheck/dry
> structure review + the code suites**, not a live boot. A live smoke is a
> Piece-7 / hands-on-hardware step.

## Error Handling

- SQLite stores raise on real errors (parity with Mongo variants) except the
  best-effort surfaces that already swallow (`search_turns`, `CheckpointStore`).
- `auth_users.email UNIQUE` — `create` on a duplicate email surfaces an error
  (parity with the admin-seed guard, which only creates when `count()==0`).
- No Mongo-reachability fallback path (deleted) — SQLite is always local/available.

## Testing

- Reuse `InMemoryUserStore` / `InMemorySessionStore` seams for existing gateway
  tests (unchanged).
- New unit tests (tmp DB file via `LocalStore(tmp_path/'state.db')`):
  - `SqliteUserStore`: create → find_by_email (case-insensitive) → count; duplicate-email error.
  - `SqliteSessionStore`: create/list/get/rename/delete/turns/add_turn/set_debug/get_debug round-trips; title+debug persist via session_kv; delete removes turns+kv.
  - `LocalStore`: `auth_user_*`, `checkpoint_*`, `delete_session` direct tests.
  - `CheckpointStore` over SQLite: put/get/delete + best-effort swallow.
- Full `tests/services/orchestrator` (CI scope) + `tests/services/ws_gateway` +
  `tests/services/local` stay green. No live E2E (GPU off).
- Grep gate: no `motor`/`pymongo`/`AsyncIOMotorClient`/`MongoUserStore`/
  `MongoSessionStore`/`db_indexes` references remain (non-comment) anywhere,
  **including** `infrastructure/local/` and the docs.
- **Infra scripts:** `shellcheck infrastructure/local/*.sh` clean (no new
  warnings vs baseline); a structural review confirms start.sh launches
  `services.local.main` (one process) and no script references mongod/redis/
  chroma. NOT live-booted (model off) — a live smoke is a Piece-7 step.
- **Docs:** a grep confirms the top-level `README.md`, `CLAUDE.md`, and
  `infrastructure/local/{README,INSTALL}.md` no longer list MongoDB/Redis/Chroma
  as required local services.

## Risks

- **Turn-shape drift** between `InMemorySessionStore` and `SqliteSessionStore` →
  frontend history breaks. Mitigation: copy the turn dict shape verbatim; a test
  asserts `turns()` output matches the InMemory store's for the same input.
- **Single-user assumption** (`LOCAL_USER`) — correct for the local harness;
  documented so a future multi-user mode revisits it.
- **`StorageManager` consumers** — verify every attribute still resolves after
  the Mongo strip (whole-suite run is the net).
