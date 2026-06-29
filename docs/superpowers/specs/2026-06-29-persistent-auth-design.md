# Design: Persistent admin auth (SQLite user store)

**Date:** 2026-06-29
**Status:** Approved (brainstorm) → ready for implementation plan
**Branch:** `feat/agents-md-memory`
**Owner:** ws_gateway

---

## Problem

On RunPod, every pod restart / fresh pod wipes the container filesystem, so the
gateway's user store starts empty. `_seed_admin()` then re-creates the admin from
`ADMIN_EMAIL` / `ADMIN_PASSWORD`, which means those env vars must be re-set every
time. The user wants a **durable admin account** that survives restarts and new
pods, so the admin env vars are set **once** and never again.

### What already exists (so we build on it, not replace it)

- `UserStore` protocol with `find_by_email` / `create` / `count`
  (`services/ws_gateway/user_store.py`).
- `InMemoryUserStore` (tests) and `MongoUserStore` (production, persists to
  `labmate.users`).
- `AuthService` with **argon2id** hashing, JWT mint/verify, and login lockout
  (5 failures → 5 min) (`services/ws_gateway/auth.py`).
- `_seed_admin()` is already idempotent: it seeds **only** when the store is empty
  AND `ADMIN_PASSWORD` is set (`services/ws_gateway/server.py:341`).
- Admin-only `POST /auth/users` endpoint to create more users.

The gap is **durability**, not the store code. The fix is a portable, file-based
store living on the one location RunPod keeps across restarts: the **persistent
network volume** (typically mounted at `/workspace`).

---

## Goals

1. The admin account **persists** across pod restarts and re-attached fresh pods
   — `ADMIN_EMAIL`/`ADMIN_PASSWORD` are needed only on the **first** init of a
   fresh volume.
2. Logins keep working across restarts (the **JWT secret persists** too).
3. **Best-available security** for a single-node, self-hosted deployment.
4. **New-user creation is disabled** for now (single admin only), behind a flag
   we can flip on later without re-architecting.

## Non-goals (explicitly out of scope for this spec)

- Multi-user management UI / CLI (`create-admin`, first-run setup screen).
- Encryption-at-rest of the DB file (SQLCipher) — noted as future.
- Migrating existing Mongo users into SQLite — fresh start is fine.
- Any change to sessions/memory storage (those stay on Mongo).

---

## Approach (chosen)

**SQLite user store on the persistent volume, plus a persisted JWT secret, plus a
user-creation lockdown flag.** SQLite is preferred over Mongo *for auth* because:

- **No network listener** — auth data is a local file, not a service on `:27017`
  that can be exposed; smallest attack surface.
- **Self-contained & portable** — one file to back up or carry between pods.
- It does **not** replace Mongo; sessions/memory still use Mongo. This is only
  the users/auth store.

### Alternatives considered

- **Keep Mongo, make it durable** (data dir on the volume, or MongoDB Atlas):
  no new store code, but ties auth uptime to Mongo and keeps a network DB to
  secure. Rejected as the default for the security reasons above.
- **External managed DB** (Atlas / Postgres): strongest durability, but adds an
  external dependency + network path + cost. Overkill for single-node now.

---

## Architecture / Components

### 1. `SqliteUserStore` (`services/ws_gateway/user_store.py`)

Implements the existing `UserStore` protocol — drop-in alongside
`MongoUserStore`/`InMemoryUserStore`, **no changes to `AuthService`**.

- Backing file: `<LABMATE_DATA_DIR>/users.db`.
- Schema: one `users` table — `id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL,
  display_name TEXT, password_hash TEXT NOT NULL, role TEXT NOT NULL,
  created_at TEXT NOT NULL`. Email stored lowercased; lookups lowercased.
- Uses the stdlib `sqlite3` driver. DB calls are synchronous but tiny; run them
  in a thread (`asyncio.to_thread`) so the async `UserStore` contract holds
  without blocking the event loop.
- On init: `mkdir -p` the data dir (mode `0700`), create the table if absent,
  and `chmod 0600` the DB file.

### 2. Store selection (`services/ws_gateway/server.py` / `config.py`)

`build_app` chooses the store from config instead of hardcoding `MongoUserStore`:

- `USER_STORE` (default `sqlite`) ∈ {`sqlite`, `mongo`, `memory`}.
- `LABMATE_DATA_DIR` — durable dir for SQLite + the JWT secret.
  Default: `/workspace/.labmate` if `/workspace` exists (RunPod), else
  `~/.labmate`.
- `mongo` keeps the current `MongoUserStore`; `memory` keeps `InMemoryUserStore`
  (still injected directly by tests).

### 3. Persisted JWT secret (`services/ws_gateway/config.py`)

Today `JWT_SECRET` defaults to `dev-insecure-secret` (forgeable; also, a per-pod
random would invalidate all tokens on restart). New behavior:

- If `JWT_SECRET` env is set, use it (operator override wins).
- Else read `<LABMATE_DATA_DIR>/jwt_secret`; if missing, generate a strong random
  secret (`secrets.token_urlsafe(48)`), write it `0600`, and reuse it forever.
- The literal `dev-insecure-secret` default is removed from the production path
  (kept only for explicit test configs).

### 4. User-creation lockdown (`services/ws_gateway/auth.py`)

- `ENABLE_USER_CREATION` (default `0`). When `0`, `POST /auth/users` returns
  `403 {"detail": "user_creation_disabled"}` **before** the admin check, so the
  capability is off regardless of role.
- The frontend does not surface any "create user" action (it already doesn't).
- Flipping to `1` restores today's admin-only create flow with zero further work.

---

## Data flow

```
fresh volume, first boot
  └─ build_app → SqliteUserStore(LABMATE_DATA_DIR/users.db)  [empty]
  └─ JWT secret: none on disk → generate → write jwt_secret (0600)
  └─ _seed_admin(): count()==0 AND ADMIN_PASSWORD set → create admin (argon2id) → persisted

later restart / new pod with same volume
  └─ SqliteUserStore opens existing users.db  [admin present]
  └─ JWT secret read from disk (same secret → existing tokens still valid)
  └─ _seed_admin(): count()>0 → no-op  (ADMIN_EMAIL/ADMIN_PASSWORD not needed)
  └─ login works against the persisted admin
```

---

## Security posture

- **argon2id** password hashing (unchanged).
- **No network-listening auth DB** — SQLite file only.
- DB file `0600`, data dir `0700`.
- **Persistent, strong JWT secret** — no `dev-insecure-secret` in prod; tokens
  stay valid across restarts; not forgeable.
- Existing **login lockout** (5 fails / 5 min) retained.
- **User creation disabled** → no account-creation surface while single-admin.
- *Future:* SQLCipher / volume encryption for at-rest secrecy of the file.

---

## Testing strategy

`pytest`, mirroring existing ws_gateway tests. No GPU/Mongo/Redis needed.

1. **`SqliteUserStore` unit** (`tests/services/ws_gateway/test_user_store_sqlite.py`)
   - create → find_by_email round-trips; email case-insensitive; `count()`.
   - **persistence across reopen**: create, drop the instance, re-open the same
     file → user still found (the core durability guarantee).
   - duplicate email raises / is rejected at the store or `create_user` layer.
   - DB file is created `0600`.
2. **Store selection** (`build_app`): `USER_STORE=sqlite` yields a SqliteUserStore
   on a tmp `LABMATE_DATA_DIR`; `memory` path unaffected; bad value → clear error.
3. **JWT secret persistence**: with no `JWT_SECRET` env and a tmp data dir, a
   secret file is generated once and reused on a second `build_app` (same value);
   explicit `JWT_SECRET` env overrides and skips the file.
4. **User-creation lockdown**: `POST /auth/users` → `403 user_creation_disabled`
   when `ENABLE_USER_CREATION` unset, even with a valid admin token; `=1` restores
   the existing create flow (admin → 201, non-admin → 403 admin_required).
5. **Seed idempotency on SQLite**: first boot with empty DB + `ADMIN_PASSWORD`
   seeds; second `build_app` on the same dir does not duplicate (count stays 1).

All new/changed `*.py` files must pass the repo's changed-file ruff gate
(`ruff check` + `ruff format --check`).

---

## Files touched

- `services/ws_gateway/user_store.py` — add `SqliteUserStore`.
- `services/ws_gateway/config.py` — `user_store`, `data_dir`, JWT-secret
  resolution, `enable_user_creation`; `from_env` additions.
- `services/ws_gateway/server.py` — store selection in `build_app`.
- `services/ws_gateway/auth.py` — `ENABLE_USER_CREATION` gate on `POST /auth/users`.
- `infrastructure/local/local.env` (+ docs) — document the new env vars and the
  `/workspace/.labmate` durable path.
- Tests as above.

## Env var summary (defaults)

| Var | Default | Meaning |
|---|---|---|
| `USER_STORE` | `sqlite` | `sqlite` \| `mongo` \| `memory` |
| `LABMATE_DATA_DIR` | `/workspace/.labmate` or `~/.labmate` | durable dir for `users.db` + `jwt_secret` |
| `JWT_SECRET` | *(generated + persisted)* | operator override; else auto |
| `ENABLE_USER_CREATION` | `0` | `1` re-enables admin-only `POST /auth/users` |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | existing | one-time first-boot admin seed |
