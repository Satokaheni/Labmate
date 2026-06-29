# Persistent Admin Auth (SQLite user store) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the ws_gateway admin account durable across RunPod restarts / fresh pods via a SQLite user store on the persistent volume, a persisted JWT secret, and a user-creation lockdown flag — so `ADMIN_EMAIL`/`ADMIN_PASSWORD` are set once, not every boot.

**Architecture:** Add a `SqliteUserStore` implementing the existing `UserStore` protocol (no `AuthService` change). `build_app` selects the store from config (`sqlite` default). `Config.from_env` resolves a durable data dir, persists/loads a strong JWT secret there, and exposes an `enable_user_creation` flag that gates `POST /auth/users`. Sessions/memory stay on Mongo.

**Tech Stack:** Python 3.11+, FastAPI, stdlib `sqlite3` (+ `asyncio.to_thread`), argon2 (existing), PyJWT (existing), pytest.

## Global Constraints

- stdout is never written in services; this is HTTP/WS code — fine to use `logging`.
- Async contract: `UserStore` methods are `async`; SQLite calls run via `asyncio.to_thread` (never block the event loop).
- New `Config` fields MUST have defaults and be appended last (frozen dataclass; existing `Config(...)` constructors in tests must keep working).
- Password hashing stays **argon2id** in `AuthService` (do not reimplement hashing in the store).
- Every new/changed `*.py` must pass the repo's changed-file ruff gate: `ruff check <files>` and `ruff format --check <files>` (ruff 0.8.6).
- Tests: `pytest` + `pytest-asyncio`; `@pytest.mark.asyncio` on async tests. No GPU/Mongo/Redis.
- File perms: data dir `0700`, secret/db files `0600`.

---

### Task 1: `SqliteUserStore`

**Files:**
- Modify: `services/ws_gateway/user_store.py` (add class + imports)
- Test: `tests/services/ws_gateway/test_user_store_sqlite.py` (create)

**Interfaces:**
- Consumes: existing `UserDoc` TypedDict and `UserStore` protocol in the same file.
- Produces: `class SqliteUserStore` with `__init__(self, db_path: str)`, and async `find_by_email(email)->Optional[UserDoc]`, `create(*, email, display_name, password_hash, role="user")->UserDoc`, `count()->int`.

- [ ] **Step 1: Write the failing test**

Create `tests/services/ws_gateway/test_user_store_sqlite.py`:

```python
import os
import stat

import pytest

from services.ws_gateway.user_store import SqliteUserStore

pytestmark = pytest.mark.asyncio


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "data" / "users.db")


async def test_create_then_find_roundtrip(db_path):
    store = SqliteUserStore(db_path)
    created = await store.create(
        email="Admin@Labmate.Local", display_name="Admin",
        password_hash="argon2$hash", role="admin",
    )
    assert created["id"].startswith("u-")
    assert created["email"] == "admin@labmate.local"  # lowercased
    found = await store.find_by_email("admin@labmate.local")
    assert found == created


async def test_find_is_case_insensitive(db_path):
    store = SqliteUserStore(db_path)
    await store.create(email="a@b.com", display_name="A", password_hash="h")
    assert (await store.find_by_email("A@B.COM"))["email"] == "a@b.com"
    assert await store.find_by_email("missing@x.com") is None


async def test_count_and_persistence_across_reopen(db_path):
    s1 = SqliteUserStore(db_path)
    assert await s1.count() == 0
    await s1.create(email="a@b.com", display_name="A", password_hash="h", role="admin")
    assert await s1.count() == 1
    # Re-open the SAME file with a fresh instance — the durability guarantee.
    s2 = SqliteUserStore(db_path)
    assert await s2.count() == 1
    assert (await s2.find_by_email("a@b.com"))["role"] == "admin"


async def test_duplicate_email_rejected(db_path):
    import sqlite3
    store = SqliteUserStore(db_path)
    await store.create(email="a@b.com", display_name="A", password_hash="h")
    with pytest.raises(sqlite3.IntegrityError):
        await store.create(email="A@B.com", display_name="A2", password_hash="h2")


async def test_db_file_is_0600(db_path):
    SqliteUserStore(db_path)
    mode = stat.S_IMODE(os.stat(db_path).st_mode)
    assert mode == 0o600
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/services/ws_gateway/test_user_store_sqlite.py -q`
Expected: FAIL — `ImportError: cannot import name 'SqliteUserStore'`.

- [ ] **Step 3: Write minimal implementation**

In `services/ws_gateway/user_store.py`, add `import asyncio`, `import os`, `import sqlite3` to the top imports (keep existing `time`, `uuid`, typing imports), then append:

```python
class SqliteUserStore:
    """File-backed store, durable across restarts. stdlib sqlite3 run in a thread
    so the async UserStore contract holds without blocking the event loop."""

    def __init__(self, db_path: str) -> None:
        self._path = db_path
        parent = os.path.dirname(db_path) or "."
        os.makedirs(parent, mode=0o700, exist_ok=True)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS users ("
                "id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, "
                "display_name TEXT, password_hash TEXT NOT NULL, "
                "role TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            conn.commit()
        finally:
            conn.close()
        try:
            os.chmod(db_path, 0o600)
        except OSError:
            pass

    def _connect(self) -> "sqlite3.Connection":
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row_to_doc(row: "sqlite3.Row") -> UserDoc:
        return {
            "id": row["id"],
            "email": row["email"],
            "displayName": row["display_name"],
            "passwordHash": row["password_hash"],
            "role": row["role"],
            "createdAt": row["created_at"],
        }

    def _find_by_email(self, email: str) -> Optional[UserDoc]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id, email, display_name, password_hash, role, created_at "
                "FROM users WHERE email = ?",
                (email,),
            ).fetchone()
        finally:
            conn.close()
        return self._row_to_doc(row) if row is not None else None

    async def find_by_email(self, email: str) -> Optional[UserDoc]:
        return await asyncio.to_thread(self._find_by_email, email.lower())

    def _insert(self, doc: UserDoc) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO users "
                "(id, email, display_name, password_hash, role, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    doc["id"],
                    doc["email"],
                    doc["displayName"],
                    doc["passwordHash"],
                    doc["role"],
                    doc["createdAt"],
                ),
            )
            conn.commit()
        finally:
            conn.close()

    async def create(
        self,
        *,
        email: str,
        display_name: str,
        password_hash: str,
        role: Literal["admin", "user"] = "user",
    ) -> UserDoc:
        doc: UserDoc = {
            "id": "u-" + uuid.uuid4().hex[:12],
            "email": email.lower(),
            "displayName": display_name,
            "passwordHash": password_hash,
            "role": role,
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        await asyncio.to_thread(self._insert, doc)
        return doc

    def _count(self) -> int:
        conn = self._connect()
        try:
            return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        finally:
            conn.close()

    async def count(self) -> int:
        return await asyncio.to_thread(self._count)
```

- [ ] **Step 4: Run tests + ruff**

Run: `python -m pytest tests/services/ws_gateway/test_user_store_sqlite.py -q`
Expected: PASS (5 tests).
Run: `ruff check services/ws_gateway/user_store.py tests/services/ws_gateway/test_user_store_sqlite.py && ruff format --check services/ws_gateway/user_store.py tests/services/ws_gateway/test_user_store_sqlite.py`
Expected: `All checks passed!` + already formatted. (If format differs, run `ruff format <files>` and re-run.)

- [ ] **Step 5: Commit**

```bash
git add services/ws_gateway/user_store.py tests/services/ws_gateway/test_user_store_sqlite.py
git commit -m "feat(ws-gateway): SqliteUserStore for durable users"
```

---

### Task 2: Persisted JWT secret resolver

**Files:**
- Modify: `services/ws_gateway/config.py` (add `import os`, `import secrets`, add function)
- Test: `tests/services/ws_gateway/test_config_secret.py` (create)

**Interfaces:**
- Produces: `def resolve_jwt_secret(env_secret: str | None, data_dir: str) -> str` in `services/ws_gateway/config.py`.

- [ ] **Step 1: Write the failing test**

Create `tests/services/ws_gateway/test_config_secret.py`:

```python
import os
import stat

from services.ws_gateway.config import resolve_jwt_secret


def test_env_secret_wins(tmp_path):
    assert resolve_jwt_secret("explicit", str(tmp_path)) == "explicit"
    assert not (tmp_path / "jwt_secret").exists()  # no file written when env given


def test_generates_and_persists_then_reuses(tmp_path):
    data_dir = str(tmp_path / "d")
    first = resolve_jwt_secret(None, data_dir)
    assert first and len(first) >= 32
    secret_file = tmp_path / "d" / "jwt_secret"
    assert secret_file.exists()
    assert stat.S_IMODE(os.stat(secret_file).st_mode) == 0o600
    # Second call reads the SAME secret back (stable across restarts).
    assert resolve_jwt_secret(None, data_dir) == first


def test_empty_env_treated_as_unset(tmp_path):
    data_dir = str(tmp_path / "d")
    s = resolve_jwt_secret("", data_dir)
    assert (tmp_path / "d" / "jwt_secret").exists()  # generated, not "" used
    assert s != ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/services/ws_gateway/test_config_secret.py -q`
Expected: FAIL — `ImportError: cannot import name 'resolve_jwt_secret'`.

- [ ] **Step 3: Write minimal implementation**

In `services/ws_gateway/config.py`, ensure the imports include `import os` (present) and add `import secrets`. Add this module-level function (above the `Config` class):

```python
def resolve_jwt_secret(env_secret: str | None, data_dir: str) -> str:
    """JWT secret resolution: explicit env wins; else load a persisted secret
    from <data_dir>/jwt_secret; else generate a strong one and persist it (0600).

    Persisting means tokens stay valid across restarts and the secret is never
    the insecure default."""
    if env_secret:
        return env_secret
    path = os.path.join(data_dir, "jwt_secret")
    try:
        with open(path, encoding="utf-8") as fh:
            existing = fh.read().strip()
        if existing:
            return existing
    except OSError:
        pass
    secret = secrets.token_urlsafe(48)
    os.makedirs(data_dir, mode=0o700, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(secret)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return secret
```

- [ ] **Step 4: Run tests + ruff**

Run: `python -m pytest tests/services/ws_gateway/test_config_secret.py -q`
Expected: PASS (3 tests).
Run: `ruff check services/ws_gateway/config.py tests/services/ws_gateway/test_config_secret.py && ruff format --check services/ws_gateway/config.py tests/services/ws_gateway/test_config_secret.py`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add services/ws_gateway/config.py tests/services/ws_gateway/test_config_secret.py
git commit -m "feat(ws-gateway): persist + reuse a strong JWT secret"
```

---

### Task 3: Config fields + `from_env` wiring

**Files:**
- Modify: `services/ws_gateway/config.py` (add fields + `from_env` logic + `_default_data_dir`)
- Test: `tests/services/ws_gateway/test_config_env.py` (create)

**Interfaces:**
- Consumes: `resolve_jwt_secret` from Task 2.
- Produces: `Config` gains `user_store: str = "sqlite"`, `data_dir: str = ""`, `enable_user_creation: bool = False`; `Config.from_env()` populates them and resolves `jwt_secret` via the data dir.

- [ ] **Step 1: Write the failing test**

Create `tests/services/ws_gateway/test_config_env.py`:

```python
from services.ws_gateway.config import Config


def test_from_env_defaults_sqlite_and_locked(monkeypatch, tmp_path):
    for var in ("USER_STORE", "ENABLE_USER_CREATION", "JWT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LABMATE_DATA_DIR", str(tmp_path))
    cfg = Config.from_env()
    assert cfg.user_store == "sqlite"
    assert cfg.enable_user_creation is False
    assert cfg.data_dir == str(tmp_path)
    # jwt secret was generated + persisted under data_dir (not the insecure default)
    assert cfg.jwt_secret != "dev-insecure-secret"
    assert (tmp_path / "jwt_secret").exists()


def test_from_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("LABMATE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("USER_STORE", "memory")
    monkeypatch.setenv("ENABLE_USER_CREATION", "1")
    monkeypatch.setenv("JWT_SECRET", "explicit-secret")
    cfg = Config.from_env()
    assert cfg.user_store == "memory"
    assert cfg.enable_user_creation is True
    assert cfg.jwt_secret == "explicit-secret"
    assert not (tmp_path / "jwt_secret").exists()  # env secret → no file
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/services/ws_gateway/test_config_env.py -q`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'user_store'`.

- [ ] **Step 3: Write minimal implementation**

In `services/ws_gateway/config.py`, add a default-data-dir helper near the top (after `resolve_jwt_secret`):

```python
def _default_data_dir() -> str:
    """RunPod keeps /workspace across restarts; fall back to ~/.labmate."""
    if os.path.isdir("/workspace"):
        return "/workspace/.labmate"
    return os.path.expanduser("~/.labmate")
```

Append three fields to the `Config` dataclass (after `mongo_url`, all with defaults):

```python
    user_store: str = "sqlite"
    data_dir: str = ""
    enable_user_creation: bool = False
```

Rewrite `from_env` so it resolves the data dir and JWT secret:

```python
    @classmethod
    def from_env(cls) -> "Config":
        origins = os.getenv("CORS_ORIGINS", "http://localhost:5173")
        data_dir = os.getenv("LABMATE_DATA_DIR", _default_data_dir())
        return cls(
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            jwt_secret=resolve_jwt_secret(os.getenv("JWT_SECRET"), data_dir),
            admin_email=os.getenv("ADMIN_EMAIL", "admin@labmate.local"),
            admin_password=os.getenv("ADMIN_PASSWORD", ""),
            jwt_expiry_seconds=int(os.getenv("JWT_EXPIRY_SECONDS", "86400")),
            cors_origins=tuple(o.strip() for o in origins.split(",") if o.strip()),
            mongo_url=os.getenv("MONGO_URL", "mongodb://localhost:27017"),
            user_store=os.getenv("USER_STORE", "sqlite"),
            data_dir=data_dir,
            enable_user_creation=os.getenv("ENABLE_USER_CREATION", "0") == "1",
        )
```

- [ ] **Step 4: Run tests + ruff**

Run: `python -m pytest tests/services/ws_gateway/test_config_env.py tests/services/ws_gateway/test_config.py -q`
Expected: PASS (new + existing config tests; existing direct `Config(...)` constructors still work via field defaults).
Run: `ruff check services/ws_gateway/config.py tests/services/ws_gateway/test_config_env.py && ruff format --check services/ws_gateway/config.py tests/services/ws_gateway/test_config_env.py`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add services/ws_gateway/config.py tests/services/ws_gateway/test_config_env.py
git commit -m "feat(ws-gateway): config for user_store, data_dir, user-creation flag"
```

---

### Task 4: Store selection in `build_app`

**Files:**
- Modify: `services/ws_gateway/server.py` (imports + `_build_user_store` + use it in `build_app`)
- Test: `tests/services/ws_gateway/test_store_selection.py` (create)

**Interfaces:**
- Consumes: `Config.user_store`, `Config.data_dir` (Task 3); `SqliteUserStore` (Task 1); `InMemoryUserStore`, `MongoUserStore` (existing).
- Produces: `def _build_user_store(config: Config) -> UserStore` in `services/ws_gateway/server.py`.

- [ ] **Step 1: Write the failing test**

Create `tests/services/ws_gateway/test_store_selection.py`:

```python
import dataclasses

import pytest

from services.ws_gateway.config import Config
from services.ws_gateway.server import _build_user_store
from services.ws_gateway.user_store import InMemoryUserStore, SqliteUserStore


def _cfg(tmp_path, **over):
    base = dict(
        redis_url="redis://x", jwt_secret="s", admin_email="a@b.c",
        admin_password="", jwt_expiry_seconds=60, cors_origins=("*",),
        mongo_url="mongodb://x", data_dir=str(tmp_path),
    )
    base.update(over)
    return Config(**base)


def test_selects_sqlite(tmp_path):
    store = _build_user_store(_cfg(tmp_path, user_store="sqlite"))
    assert isinstance(store, SqliteUserStore)
    assert (tmp_path / "users.db").exists()


def test_selects_memory(tmp_path):
    assert isinstance(_build_user_store(_cfg(tmp_path, user_store="memory")), InMemoryUserStore)


def test_unknown_store_raises(tmp_path):
    with pytest.raises(ValueError, match="unknown USER_STORE"):
        _build_user_store(_cfg(tmp_path, user_store="bogus"))


@pytest.mark.asyncio
async def test_seed_idempotent_across_reopen(tmp_path):
    # First store: empty → admin would be seeded. Second store on same dir: count
    # stays 1, so _seed_admin's `count()==0` guard skips (no re-seed needed).
    s1 = _build_user_store(_cfg(tmp_path, user_store="sqlite"))
    await s1.create(email="a@b.c", display_name="Admin", password_hash="h", role="admin")
    s2 = _build_user_store(_cfg(tmp_path, user_store="sqlite"))
    assert await s2.count() == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/services/ws_gateway/test_store_selection.py -q`
Expected: FAIL — `ImportError: cannot import name '_build_user_store'`.

- [ ] **Step 3: Write minimal implementation**

In `services/ws_gateway/server.py`: add `import os` to the top imports, and extend the user_store import line to:

```python
from services.ws_gateway.user_store import (
    InMemoryUserStore,
    MongoUserStore,
    SqliteUserStore,
    UserStore,
)
```

Add the selector function above `build_app`:

```python
def _build_user_store(config: Config) -> UserStore:
    kind = config.user_store
    if kind == "memory":
        return InMemoryUserStore()
    if kind == "mongo":
        return MongoUserStore(config.mongo_url)
    if kind == "sqlite":
        return SqliteUserStore(os.path.join(config.data_dir, "users.db"))
    raise ValueError(f"unknown USER_STORE: {kind!r}")
```

In `build_app`, replace the line
`user_store = user_store or MongoUserStore(config.mongo_url)`
with:

```python
    if user_store is None:
        user_store = _build_user_store(config)
```

- [ ] **Step 4: Run tests + ruff**

Run: `python -m pytest tests/services/ws_gateway/test_store_selection.py tests/services/ws_gateway/test_server.py -q`
Expected: PASS (new + existing server tests; existing tests inject `user_store` directly so selection is bypassed for them).
Run: `ruff check services/ws_gateway/server.py tests/services/ws_gateway/test_store_selection.py && ruff format --check services/ws_gateway/server.py tests/services/ws_gateway/test_store_selection.py`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add services/ws_gateway/server.py tests/services/ws_gateway/test_store_selection.py
git commit -m "feat(ws-gateway): select user store from config (sqlite default)"
```

---

### Task 5: User-creation lockdown

**Files:**
- Modify: `services/ws_gateway/auth.py` (add `user_creation_enabled` property + gate the endpoint)
- Test: `tests/services/ws_gateway/test_user_creation_lockdown.py` (create)

**Interfaces:**
- Consumes: `Config.enable_user_creation` (Task 3); `InMemoryUserStore` (existing).
- Produces: `AuthService.user_creation_enabled: bool` property; `POST /auth/users` returns `403 {"detail": "user_creation_disabled"}` when disabled (checked before the admin check).

- [ ] **Step 1: Write the failing test**

Create `tests/services/ws_gateway/test_user_creation_lockdown.py`:

```python
import dataclasses

import pytest
from fastapi.testclient import TestClient
from fastapi import FastAPI

from services.ws_gateway.auth import AuthService, build_auth_router
from services.ws_gateway.config import Config
from services.ws_gateway.user_store import InMemoryUserStore

pytestmark = pytest.mark.asyncio


def _cfg(**over):
    base = dict(
        redis_url="r", jwt_secret="test-secret", admin_email="admin@x.com",
        admin_password="pw", jwt_expiry_seconds=60, cors_origins=("*",),
        mongo_url="m",
    )
    base.update(over)
    return Config(**base)


async def _client_with_admin(cfg):
    store = InMemoryUserStore()
    auth = AuthService(cfg, store)
    admin = await auth.create_user("admin@x.com", "pw", "Admin", role="admin")
    token = auth.mint_token(admin)
    app = FastAPI()
    app.include_router(build_auth_router(auth))
    return TestClient(app), token


async def test_create_user_disabled_returns_403(self_unused=None):
    cfg = _cfg(enable_user_creation=False)
    client, token = await _client_with_admin(cfg)
    r = client.post(
        "/auth/users",
        json={"email": "new@x.com", "password": "pw2", "displayName": "New"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "user_creation_disabled"


async def test_create_user_enabled_admin_succeeds():
    cfg = _cfg(enable_user_creation=True)
    client, token = await _client_with_admin(cfg)
    r = client.post(
        "/auth/users",
        json={"email": "new@x.com", "password": "pw2", "displayName": "New"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201
    assert r.json()["email"] == "new@x.com"


async def test_create_user_enabled_non_admin_forbidden():
    cfg = _cfg(enable_user_creation=True)
    client, _admin_token = await _client_with_admin(cfg)
    r = client.post(
        "/auth/users",
        json={"email": "n@x.com", "password": "p", "displayName": "N"},
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "admin_required"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/services/ws_gateway/test_user_creation_lockdown.py -q`
Expected: FAIL — the disabled test gets `201`/`admin_required` instead of `user_creation_disabled` (no gate yet).

- [ ] **Step 3: Write minimal implementation**

In `services/ws_gateway/auth.py`, add a property to `AuthService` (near `is_locked`):

```python
    @property
    def user_creation_enabled(self) -> bool:
        return self._cfg.enable_user_creation
```

In `build_auth_router`, gate the `create_user` route — add the check as the FIRST line of the handler body, before the token/admin check:

```python
    @router.post("/auth/users", status_code=201)
    async def create_user(body: CreateUserBody, authorization: str = Header(default="")) -> dict:
        if not service.user_creation_enabled:
            raise HTTPException(status_code=403, detail="user_creation_disabled")
        token = authorization.removeprefix("Bearer ").strip()
        claims = service.verify_token(token)
        if not claims or claims.get("role") != "admin":
            raise HTTPException(status_code=403, detail="admin_required")
        user = await service.create_user(body.email, body.password, body.displayName, role="user")
        return {"id": user["id"], "email": user["email"]}
```

- [ ] **Step 4: Run tests + ruff**

Run: `python -m pytest tests/services/ws_gateway/test_user_creation_lockdown.py tests/services/ws_gateway/test_auth.py -q`
Expected: PASS. NOTE: if `tests/services/ws_gateway/test_auth.py` has an existing "create user" test that relied on creation being on, it will now hit the gate — update that test's `Config` to `enable_user_creation=True` so it exercises the enabled path. Make that edit if the run reports it.
Run: `ruff check services/ws_gateway/auth.py tests/services/ws_gateway/test_user_creation_lockdown.py && ruff format --check services/ws_gateway/auth.py tests/services/ws_gateway/test_user_creation_lockdown.py`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add services/ws_gateway/auth.py tests/services/ws_gateway/test_user_creation_lockdown.py
git commit -m "feat(ws-gateway): gate POST /auth/users behind ENABLE_USER_CREATION"
```

---

### Task 6: Document the new env vars

**Files:**
- Modify: `infrastructure/local/local.env` (append a documented block)

**Interfaces:** none (docs only).

- [ ] **Step 1: Append the env documentation**

Append to `infrastructure/local/local.env` (adjust to the file's existing comment style; keep these as real or commented exports as the file does for other optional vars):

```bash
# ── Auth persistence (ws_gateway) ─────────────────────────────────────────────
# Durable user store + JWT secret live under LABMATE_DATA_DIR. On RunPod, point
# this at the persistent network volume (/workspace) so the admin survives pod
# restarts and ADMIN_EMAIL/ADMIN_PASSWORD are only needed on the FIRST init.
export USER_STORE=sqlite                    # sqlite | mongo | memory
export LABMATE_DATA_DIR=/workspace/.labmate # holds users.db + jwt_secret (0600)
export ENABLE_USER_CREATION=0               # 1 re-enables admin-only POST /auth/users
# JWT_SECRET is auto-generated + persisted under LABMATE_DATA_DIR if unset.
# ADMIN_EMAIL / ADMIN_PASSWORD seed the admin ONCE on a fresh volume.
```

- [ ] **Step 2: Commit**

```bash
git add infrastructure/local/local.env
git commit -m "docs(ws-gateway): document auth-persistence env vars"
```

---

## Self-Review

**Spec coverage:**
- SqliteUserStore → Task 1. ✓
- Store selection (USER_STORE/LABMATE_DATA_DIR) → Tasks 3 (config) + 4 (selection). ✓
- Persisted JWT secret → Tasks 2 (resolver) + 3 (wired in from_env). ✓
- User-creation lockdown → Task 5. ✓
- Tests: SqliteUserStore unit incl. persistence + 0600 → Task 1; store selection → Task 4; jwt persistence → Task 2; lockdown → Task 5; seed idempotency → Task 4 (`test_seed_idempotent_across_reopen`). ✓
- Docs / local.env → Task 6. ✓
- Non-goals (CLI, first-run UI, encryption-at-rest, Mongo migration) correctly excluded.

**Placeholder scan:** none — every step has complete code/commands.

**Type consistency:** `SqliteUserStore(db_path)` and `UserDoc` keys (`displayName`/`passwordHash`/`createdAt`) match `user_store.py`; `resolve_jwt_secret(env_secret, data_dir)` signature identical in Tasks 2/3; `_build_user_store(config)` returns `UserStore`; `Config` field names (`user_store`, `data_dir`, `enable_user_creation`) consistent across Tasks 3/4/5.

**Note for executor:** existing tests construct `Config(...)` directly — the new fields have defaults so those keep compiling; only a pre-existing "create user" test (if any in `test_auth.py`) needs `enable_user_creation=True` (flagged in Task 5 Step 4).
