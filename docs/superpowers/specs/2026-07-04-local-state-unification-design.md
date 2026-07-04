# Local State Unification (Mongo → SQLite) — Design

**Date:** 2026-07-04
**Branch target:** `experimental` (pod version on `main` untouched)
**Piece:** "Memory piece" (state), sequenced **before** Piece 7 packaging (per user decision 2026-07-04).
**Execution:** subagent-driven-development (haiku implement → opus review).

## Goal

Drop **MongoDB** as a boot dependency of the local single-process harness
(`services/local/main.py`) so the only remote dependency is the model endpoint
(`GEMMA_BASE`). All persistent state moves to the existing SQLite `LocalStore`,
which becomes the **single source of truth** read by both the orchestrator and
the ws_gateway.

## Non-Goals / Out of Scope

- **Markdown long-term memory** (`remember`/`recall`, MEMORY.md tooling) — still
  deferred to **post-Piece-7** (per `project_local_memory_model`). This piece is
  STATE only (sessions, auth users, turns, loop checkpoints).
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
  `MongoSessionStore`/`db_indexes` references remain (non-comment).

## Risks

- **Turn-shape drift** between `InMemorySessionStore` and `SqliteSessionStore` →
  frontend history breaks. Mitigation: copy the turn dict shape verbatim; a test
  asserts `turns()` output matches the InMemory store's for the same input.
- **Single-user assumption** (`LOCAL_USER`) — correct for the local harness;
  documented so a future multi-user mode revisits it.
- **`StorageManager` consumers** — verify every attribute still resolves after
  the Mongo strip (whole-suite run is the net).
