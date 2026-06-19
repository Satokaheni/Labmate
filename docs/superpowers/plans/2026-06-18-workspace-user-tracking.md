# Workspace & User Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add workspace and user identity to Labmate so sessions are scoped to a named project (with one or more code paths + optional research corpus + instructions) and tied to a stable user identity.

**Architecture:** A `workspaces` and `users` MongoDB collection; every session carries `(user_id, workspace_id, session_id)`; `StorageManager` gains workspace/user CRUD; `main.py` payload expanded; LangGraph config carries workspace context. No auth system yet — user is identified by a display name chosen at first run, stored locally.

**Tech Stack:** Python, Motor (async MongoDB), Redis Streams, LangGraph configurable, Pydantic models.

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `services/orchestrator/models.py` | Create | Pydantic models: `User`, `Workspace`, `SessionMeta` |
| `services/orchestrator/workspace_manager.py` | Create | CRUD for workspaces and users |
| `services/orchestrator/storage_manager.py` | Modify | Add `create_workspace`, `get_workspace`, `create_user`, `get_user`, `list_workspaces`, `list_sessions` |
| `services/orchestrator/main.py` | Modify | Accept `user_id` + `workspace_id` in payload; pass to `run_task` |
| `services/orchestrator/coding_orchestrator.py` | Modify | `run_task` accepts `user_id`, `workspace_id`; passes them in LangGraph config |
| `services/orchestrator/graph.py` | Modify | Thread config carries `workspace_id`, `user_id` in `configurable` |
| `services/orchestrator/types.py` | Modify | `State` gains `workspace_id: str`, `user_id: str` |
| `tests/services/orchestrator/test_workspace_manager.py` | Create | Tests for workspace/user CRUD |
| `tests/services/orchestrator/test_models.py` | Create | Pydantic model validation tests |

---

### Task 1: Add Pydantic models

**Files:**
- Create: `services/orchestrator/models.py`
- Test: `tests/services/orchestrator/test_models.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/orchestrator/test_models.py
from services.orchestrator.models import User, Workspace, SessionMeta
import pytest
from datetime import datetime, timezone

def test_user_defaults():
    u = User(display_name="Alice")
    assert u.user_id
    assert u.created_at <= datetime.now(timezone.utc)

def test_workspace_defaults():
    ws = Workspace(name="my-project", user_id="u-123")
    assert ws.workspace_id
    assert ws.paths == []
    assert ws.sources == []
    assert ws.instructions == ""

def test_workspace_with_paths():
    ws = Workspace(
        name="ml-research",
        user_id="u-123",
        paths=["/workspace/myrepo", "/workspace/other"],
        sources=["arxiv:2401.12345"],
        instructions="Focus on Python. Use type hints.",
    )
    assert len(ws.paths) == 2
    assert "arxiv" in ws.sources[0]

def test_session_meta():
    sm = SessionMeta(
        session_id="s-abc",
        user_id="u-123",
        workspace_id="ws-456",
        task_preview="Write a sorting algorithm",
    )
    assert sm.session_id == "s-abc"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_models.py -v
```
Expected: `ModuleNotFoundError: No module named 'services.orchestrator.models'`

- [ ] **Step 3: Implement models.py**

```python
# services/orchestrator/models.py
from __future__ import annotations
import uuid
from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)

def _uid() -> str:
    return str(uuid.uuid4())


class User(BaseModel):
    user_id: str = Field(default_factory=_uid)
    display_name: str
    created_at: datetime = Field(default_factory=_now)
    last_active: datetime = Field(default_factory=_now)


class Workspace(BaseModel):
    workspace_id: str = Field(default_factory=_uid)
    name: str
    user_id: str
    description: str = ""
    paths: list[str] = Field(default_factory=list)      # local filesystem paths (code dirs)
    sources: list[str] = Field(default_factory=list)    # research sources (URLs, arxiv IDs, etc.)
    instructions: str = ""                              # per-workspace system prompt layer
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class SessionMeta(BaseModel):
    session_id: str
    user_id: str
    workspace_id: str
    task_preview: str = ""          # first 120 chars of the task
    created_at: datetime = Field(default_factory=_now)
    completed_at: Optional[datetime] = None
    ok: bool = True
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/services/orchestrator/test_models.py -v
```
Expected: 4 tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/models.py tests/services/orchestrator/test_models.py
git commit -m "feat(orchestrator): add User, Workspace, SessionMeta pydantic models"
```

---

### Task 2: WorkspaceManager — CRUD layer

**Files:**
- Create: `services/orchestrator/workspace_manager.py`
- Test: `tests/services/orchestrator/test_workspace_manager.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/orchestrator/test_workspace_manager.py
from __future__ import annotations
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock
from services.orchestrator.workspace_manager import WorkspaceManager
from services.orchestrator.models import User, Workspace


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.__getitem__ = MagicMock(return_value=AsyncMock())
    return db


@pytest.fixture
def mgr(mock_db):
    return WorkspaceManager(mock_db)


@pytest.mark.asyncio
async def test_create_user(mgr, mock_db):
    mock_db["users"].insert_one = AsyncMock(return_value=MagicMock(inserted_id="abc"))
    user = await mgr.create_user("Alice")
    assert user.display_name == "Alice"
    assert user.user_id
    mock_db["users"].insert_one.assert_called_once()


@pytest.mark.asyncio
async def test_create_workspace(mgr, mock_db):
    mock_db["workspaces"].insert_one = AsyncMock(return_value=MagicMock(inserted_id="abc"))
    ws = await mgr.create_workspace(
        user_id="u-1",
        name="my-lab",
        paths=["/workspace/myrepo"],
        instructions="Be concise.",
    )
    assert ws.name == "my-lab"
    assert ws.user_id == "u-1"
    assert ws.paths == ["/workspace/myrepo"]
    mock_db["workspaces"].insert_one.assert_called_once()


@pytest.mark.asyncio
async def test_get_workspace_not_found(mgr, mock_db):
    mock_db["workspaces"].find_one = AsyncMock(return_value=None)
    result = await mgr.get_workspace("nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_list_workspaces(mgr, mock_db):
    cursor = MagicMock()
    cursor.to_list = AsyncMock(return_value=[
        {"workspace_id": "ws-1", "name": "proj-a", "user_id": "u-1",
         "paths": [], "sources": [], "instructions": "",
         "description": "", "created_at": None, "updated_at": None},
    ])
    mock_db["workspaces"].find = MagicMock(return_value=cursor)
    result = await mgr.list_workspaces("u-1")
    assert len(result) == 1
    assert result[0].name == "proj-a"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/services/orchestrator/test_workspace_manager.py -v
```
Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement WorkspaceManager**

```python
# services/orchestrator/workspace_manager.py
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from .models import User, Workspace, SessionMeta

logger = logging.getLogger(__name__)

USERS = "users"
WORKSPACES = "workspaces"
SESSIONS = "sessions"


class WorkspaceManager:
    """CRUD layer for users, workspaces, and session metadata."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._db = db

    # ── users ─────────────────────────────────────────────────────────────

    async def create_user(self, display_name: str) -> User:
        user = User(display_name=display_name)
        await self._db[USERS].insert_one(user.model_dump())
        logger.info("created user %s (%s)", user.user_id, display_name)
        return user

    async def get_user(self, user_id: str) -> Optional[User]:
        doc = await self._db[USERS].find_one({"user_id": user_id})
        return User(**doc) if doc else None

    async def touch_user(self, user_id: str) -> None:
        await self._db[USERS].update_one(
            {"user_id": user_id},
            {"$set": {"last_active": datetime.now(timezone.utc)}},
        )

    # ── workspaces ────────────────────────────────────────────────────────

    async def create_workspace(
        self,
        user_id: str,
        name: str,
        paths: list[str] | None = None,
        sources: list[str] | None = None,
        description: str = "",
        instructions: str = "",
    ) -> Workspace:
        ws = Workspace(
            name=name,
            user_id=user_id,
            paths=paths or [],
            sources=sources or [],
            description=description,
            instructions=instructions,
        )
        await self._db[WORKSPACES].insert_one(ws.model_dump())
        logger.info("created workspace %s (%s)", ws.workspace_id, name)
        return ws

    async def get_workspace(self, workspace_id: str) -> Optional[Workspace]:
        doc = await self._db[WORKSPACES].find_one({"workspace_id": workspace_id})
        return Workspace(**doc) if doc else None

    async def list_workspaces(self, user_id: str) -> list[Workspace]:
        cursor = self._db[WORKSPACES].find({"user_id": user_id})
        docs = await cursor.to_list(length=100)
        return [Workspace(**d) for d in docs]

    async def update_workspace(self, workspace_id: str, **fields) -> None:
        fields["updated_at"] = datetime.now(timezone.utc)
        await self._db[WORKSPACES].update_one(
            {"workspace_id": workspace_id},
            {"$set": fields},
        )

    # ── session metadata ──────────────────────────────────────────────────

    async def record_session(self, meta: SessionMeta) -> None:
        await self._db[SESSIONS].insert_one(meta.model_dump())

    async def complete_session(self, session_id: str, ok: bool = True) -> None:
        await self._db[SESSIONS].update_one(
            {"session_id": session_id},
            {"$set": {"completed_at": datetime.now(timezone.utc), "ok": ok}},
        )

    async def list_sessions(
        self,
        user_id: str,
        workspace_id: str | None = None,
        limit: int = 20,
    ) -> list[SessionMeta]:
        q: dict = {"user_id": user_id}
        if workspace_id:
            q["workspace_id"] = workspace_id
        cursor = self._db[SESSIONS].find(q).sort("created_at", -1).limit(limit)
        docs = await cursor.to_list(length=limit)
        return [SessionMeta(**d) for d in docs]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/services/orchestrator/test_workspace_manager.py -v
```
Expected: 4 tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/workspace_manager.py tests/services/orchestrator/test_workspace_manager.py
git commit -m "feat(orchestrator): add WorkspaceManager with user/workspace/session CRUD"
```

---

### Task 3: Add workspace/user CRUD to StorageManager

**Files:**
- Modify: `services/orchestrator/storage_manager.py`
- Test: `tests/services/orchestrator/test_storage_manager.py` (extend existing)

- [ ] **Step 1: Write the failing tests** (add to end of existing test file)

```python
# add to tests/services/orchestrator/test_storage_manager.py

@pytest.mark.asyncio
async def test_storage_manager_has_workspace_manager(sm):
    """StorageManager exposes a WorkspaceManager via .workspaces property."""
    from services.orchestrator.workspace_manager import WorkspaceManager
    assert isinstance(sm.workspaces, WorkspaceManager)

@pytest.mark.asyncio
async def test_storage_manager_workspace_manager_uses_same_db(sm):
    """WorkspaceManager receives the same db instance StorageManager uses."""
    assert sm.workspaces._db is sm._db
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/services/orchestrator/test_storage_manager.py -v -k "workspace_manager"
```
Expected: `AttributeError: 'StorageManager' object has no attribute 'workspaces'`

- [ ] **Step 3: Add WorkspaceManager to StorageManager**

In `services/orchestrator/storage_manager.py`, add to `__init__` (after `self._db = self._mongo[DB_NAME]`):

```python
from .workspace_manager import WorkspaceManager
self._workspaces = WorkspaceManager(self._db)
```

Add property after `__aexit__`:
```python
@property
def workspaces(self) -> "WorkspaceManager":
    return self._workspaces
```

Also update `from_clients` class method to initialise it:
```python
self._workspaces = WorkspaceManager(mongo[DB_NAME])
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/services/orchestrator/test_storage_manager.py -v
```
Expected: all PASSED.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/storage_manager.py tests/services/orchestrator/test_storage_manager.py
git commit -m "feat(orchestrator): expose WorkspaceManager via StorageManager.workspaces"
```

---

### Task 4: Update State, run_task, and LangGraph config

**Files:**
- Modify: `services/orchestrator/types.py`
- Modify: `services/orchestrator/coding_orchestrator.py`
- Modify: `services/orchestrator/graph.py`

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/services/orchestrator/test_coding_orchestrator.py

@pytest.mark.asyncio
async def test_run_task_accepts_user_workspace(orch_with_graph):
    """run_task accepts user_id and workspace_id without error."""
    state = await orch_with_graph.run_task(
        "hello",
        session_id="s-1",
        user_id="u-abc",
        workspace_id="ws-xyz",
    )
    assert isinstance(state, dict)

@pytest.mark.asyncio
async def test_state_carries_workspace_fields(orch_with_graph):
    """Final state includes workspace_id and user_id."""
    state = await orch_with_graph.run_task(
        "hello",
        session_id="s-2",
        user_id="u-abc",
        workspace_id="ws-xyz",
    )
    assert state.get("workspace_id") == "ws-xyz"
    assert state.get("user_id") == "u-abc"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/services/orchestrator/test_coding_orchestrator.py -v -k "workspace"
```

- [ ] **Step 3: Update types.py — add fields to State**

```python
# In services/orchestrator/types.py, inside class State(TypedDict, total=False):
workspace_id: str       # which workspace this session belongs to
user_id: str            # stable user identifier
```

- [ ] **Step 4: Update coding_orchestrator.py — run_task signature**

Change `run_task` signature:
```python
async def run_task(
    self,
    task: str,
    session_id: str,
    user_id: str = "",
    workspace_id: str = "",
) -> dict:
```

Add to the initial state dict inside `run_task`:
```python
"workspace_id": workspace_id,
"user_id": user_id,
```

- [ ] **Step 5: Update graph.py — pass workspace/user in LangGraph configurable**

In the graph invocation inside `run_task`, add to `config`:
```python
config = {
    "configurable": {
        "thread_id": session_id,
        "workspace_id": workspace_id,
        "user_id": user_id,
    }
}
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
python -m pytest tests/services/orchestrator/ -v -k "workspace or user_id"
```
Expected: all PASSED.

- [ ] **Step 7: Commit**

```bash
git add services/orchestrator/types.py services/orchestrator/coding_orchestrator.py services/orchestrator/graph.py
git commit -m "feat(orchestrator): thread workspace_id and user_id through State and LangGraph config"
```

---

### Task 5: Update main.py payload and session recording

**Files:**
- Modify: `services/orchestrator/main.py`
- Test: `tests/services/orchestrator/test_main.py` (extend existing)

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/services/orchestrator/test_main.py

@pytest.mark.asyncio
async def test_handle_parses_user_and_workspace(proc, mock_redis, mock_orch):
    """_handle extracts user_id and workspace_id from payload."""
    fields = {"payload": json.dumps({
        "task_id": "t-1",
        "task": "do something",
        "session_id": "s-1",
        "user_id": "u-abc",
        "workspace_id": "ws-xyz",
    })}
    await proc._handle("msg-1", fields, mock_orch)
    call_kwargs = mock_orch.run_task.call_args
    assert call_kwargs.kwargs.get("user_id") == "u-abc"
    assert call_kwargs.kwargs.get("workspace_id") == "ws-xyz"

@pytest.mark.asyncio
async def test_handle_defaults_missing_user_workspace(proc, mock_redis, mock_orch):
    """Missing user_id/workspace_id default to empty string without error."""
    fields = {"payload": json.dumps({"task_id": "t-2", "task": "hi", "session_id": "s-2"})}
    await proc._handle("msg-2", fields, mock_orch)
    call_kwargs = mock_orch.run_task.call_args
    assert call_kwargs.kwargs.get("user_id") == ""
    assert call_kwargs.kwargs.get("workspace_id") == ""
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/services/orchestrator/test_main.py -v -k "user_and_workspace or missing_user"
```

- [ ] **Step 3: Update main.py `_handle`**

In `OrchestratorProcess._handle`, extract two new fields from payload:
```python
user_id      = payload.get("user_id", "")
workspace_id = payload.get("workspace_id", "")
```

Pass them to `run_task`:
```python
final_state = await orch.run_task(
    task_text,
    session_id,
    user_id=user_id,
    workspace_id=workspace_id,
)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/services/orchestrator/test_main.py -v
```

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/main.py tests/services/orchestrator/test_main.py
git commit -m "feat(orchestrator): extract user_id and workspace_id from goal payload"
```

---

### Task 6: MongoDB indexes

**Files:**
- Create: `services/orchestrator/db_indexes.py`

No tests needed — this is idempotent DDL. Run once at startup.

- [ ] **Step 1: Implement db_indexes.py**

```python
# services/orchestrator/db_indexes.py
"""Create MongoDB indexes on first run. Safe to call repeatedly (idempotent)."""
from __future__ import annotations
import logging
from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)


async def ensure_indexes(db: AsyncIOMotorDatabase) -> None:
    await db["users"].create_index("user_id", unique=True)
    await db["workspaces"].create_index("workspace_id", unique=True)
    await db["workspaces"].create_index([("user_id", 1), ("name", 1)])
    await db["sessions"].create_index("session_id", unique=True)
    await db["sessions"].create_index([("user_id", 1), ("workspace_id", 1), ("created_at", -1)])
    await db["episodes"].create_index([("session_id", 1), ("seq", 1)])
    await db["memories"].create_index([("session_id", 1), ("valid_to", 1)])
    logger.info("MongoDB indexes ensured")
```

- [ ] **Step 2: Call it from StorageManager.__aenter__**

Add to `storage_manager.py` `__aenter__`:
```python
from .db_indexes import ensure_indexes
await ensure_indexes(self._db)
```

- [ ] **Step 3: Commit**

```bash
git add services/orchestrator/db_indexes.py services/orchestrator/storage_manager.py
git commit -m "feat(orchestrator): add MongoDB index definitions, run at startup"
```
