# CLI Connector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `labmate` CLI that feels like Claude Code — interactive REPL, streaming output, workspace selection, session resume, `!<cmd>` shell passthrough, slash commands. Interacts with the orchestrator via Redis (same interface any future connector will use).

**Architecture:** `services/cli/` Python package. On startup: select/create workspace, then enter REPL loop. Each prompt pushes `{ task_id, task, session_id, user_id, workspace_id }` to `labmate:goals` Redis stream, then subscribes to `labmate:result:<task_id>` pubsub and streams result to terminal with Rich rendering.

**Tech Stack:** Python, `rich` (rendering), `redis.asyncio` (stream push + pubsub), `prompt_toolkit` (REPL input with history), `typer` (CLI args), `asyncio`.

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `services/cli/__init__.py` | Create | Package marker |
| `services/cli/main.py` | Create | Entry point — arg parsing, startup sequence, hands off to REPL |
| `services/cli/repl.py` | Create | Async REPL loop — prompt, send, stream result |
| `services/cli/renderer.py` | Create | Rich Markdown rendering + spinner + streaming print |
| `services/cli/redis_client.py` | Create | Push to `labmate:goals`; subscribe and read from `labmate:result:<id>` |
| `services/cli/identity.py` | Create | Load/save local user identity (`~/.labmate/identity.json`) |
| `services/cli/workspace_picker.py` | Create | Interactive workspace select/create at startup |
| `services/cli/session_store.py` | Create | Local session history (`~/.labmate/sessions.jsonl`) for resume |
| `services/cli/requirements.txt` | Create | rich, prompt_toolkit, typer, redis[asyncio] |
| `tests/services/cli/test_redis_client.py` | Create | Unit tests for push/subscribe logic |
| `tests/services/cli/test_renderer.py` | Create | Renderer output tests |
| `tests/services/cli/test_identity.py` | Create | Identity load/save tests |
| `tests/services/cli/test_session_store.py` | Create | Session history tests |

---

### Task 1: Package scaffold and requirements

**Files:**
- Create: `services/cli/__init__.py`
- Create: `services/cli/requirements.txt`
- Create: `tests/services/cli/__init__.py`

- [ ] **Step 1: Create files**

```bash
mkdir -p services/cli tests/services/cli
touch services/cli/__init__.py tests/services/cli/__init__.py
```

`services/cli/requirements.txt`:
```
rich>=13.7.0
prompt_toolkit>=3.0.43
typer>=0.12.0
redis[asyncio]>=5.0.0
```

- [ ] **Step 2: Commit**

```bash
git add services/cli/ tests/services/cli/
git commit -m "feat(cli): scaffold cli package"
```

---

### Task 2: Identity — local user profile

**Files:**
- Create: `services/cli/identity.py`
- Test: `tests/services/cli/test_identity.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/cli/test_identity.py
from __future__ import annotations
import json
import pytest
from pathlib import Path
from unittest.mock import patch, mock_open
from services.cli.identity import Identity, load_or_create_identity


def test_identity_serializes():
    ident = Identity(user_id="u-1", display_name="Alice")
    d = ident.to_dict()
    assert d["user_id"] == "u-1"
    assert d["display_name"] == "Alice"


def test_identity_round_trips(tmp_path):
    ident = Identity(user_id="u-2", display_name="Bob")
    path = tmp_path / "identity.json"
    ident.save(path)
    loaded = Identity.load(path)
    assert loaded.user_id == "u-2"
    assert loaded.display_name == "Bob"


def test_load_or_create_new(tmp_path, monkeypatch):
    monkeypatch.setattr("services.cli.identity.IDENTITY_PATH", tmp_path / "identity.json")
    monkeypatch.setattr("builtins.input", lambda _: "Charlie")
    ident = load_or_create_identity()
    assert ident.display_name == "Charlie"
    assert (tmp_path / "identity.json").exists()


def test_load_or_create_existing(tmp_path, monkeypatch):
    path = tmp_path / "identity.json"
    path.write_text(json.dumps({"user_id": "u-3", "display_name": "Dave"}))
    monkeypatch.setattr("services.cli.identity.IDENTITY_PATH", path)
    ident = load_or_create_identity()
    assert ident.user_id == "u-3"
    assert ident.display_name == "Dave"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/services/cli/test_identity.py -v
```
Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement identity.py**

```python
# services/cli/identity.py
from __future__ import annotations
import json
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path

IDENTITY_PATH = Path.home() / ".labmate" / "identity.json"


@dataclass
class Identity:
    user_id: str
    display_name: str

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, path: Path = IDENTITY_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path: Path = IDENTITY_PATH) -> "Identity":
        data = json.loads(path.read_text())
        return cls(**data)


def load_or_create_identity(path: Path = IDENTITY_PATH) -> Identity:
    if path.exists():
        return Identity.load(path)
    name = input("Welcome to Labmate! Enter your display name: ").strip() or "User"
    ident = Identity(user_id=str(uuid.uuid4()), display_name=name)
    ident.save(path)
    return ident
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/services/cli/test_identity.py -v
```
Expected: 4 tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add services/cli/identity.py tests/services/cli/test_identity.py
git commit -m "feat(cli): local user identity with load-or-create flow"
```

---

### Task 3: Session store — local history

**Files:**
- Create: `services/cli/session_store.py`
- Test: `tests/services/cli/test_session_store.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/cli/test_session_store.py
from __future__ import annotations
import pytest
from services.cli.session_store import SessionStore, SessionRecord


def test_append_and_list(tmp_path):
    store = SessionStore(tmp_path / "sessions.jsonl")
    store.append(SessionRecord(
        session_id="s-1",
        workspace_id="ws-1",
        workspace_name="my-lab",
        task_preview="Write hello world",
    ))
    store.append(SessionRecord(
        session_id="s-2",
        workspace_id="ws-1",
        workspace_name="my-lab",
        task_preview="Sort a list",
    ))
    sessions = store.list()
    assert len(sessions) == 2
    assert sessions[0].session_id == "s-2"   # most recent first


def test_list_by_workspace(tmp_path):
    store = SessionStore(tmp_path / "sessions.jsonl")
    store.append(SessionRecord(session_id="s-1", workspace_id="ws-1", workspace_name="a", task_preview="x"))
    store.append(SessionRecord(session_id="s-2", workspace_id="ws-2", workspace_name="b", task_preview="y"))
    result = store.list(workspace_id="ws-1")
    assert len(result) == 1
    assert result[0].session_id == "s-1"


def test_empty_store(tmp_path):
    store = SessionStore(tmp_path / "sessions.jsonl")
    assert store.list() == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/services/cli/test_session_store.py -v
```

- [ ] **Step 3: Implement session_store.py**

```python
# services/cli/session_store.py
from __future__ import annotations
import json
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

SESSION_PATH = Path.home() / ".labmate" / "sessions.jsonl"


@dataclass
class SessionRecord:
    session_id: str
    workspace_id: str
    workspace_name: str
    task_preview: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class SessionStore:
    def __init__(self, path: Path = SESSION_PATH) -> None:
        self._path = path

    def append(self, record: SessionRecord) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a") as f:
            f.write(json.dumps(asdict(record)) + "\n")

    def list(self, workspace_id: str | None = None, limit: int = 50) -> list[SessionRecord]:
        if not self._path.exists():
            return []
        records = []
        for line in self._path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if workspace_id and d.get("workspace_id") != workspace_id:
                    continue
                records.append(SessionRecord(**d))
            except (json.JSONDecodeError, TypeError):
                continue
        return list(reversed(records))[-limit:]  # most-recent first
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/services/cli/test_session_store.py -v
```
Expected: 3 tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add services/cli/session_store.py tests/services/cli/test_session_store.py
git commit -m "feat(cli): local session history store (append-only JSONL)"
```

---

### Task 4: Redis client — push and subscribe

**Files:**
- Create: `services/cli/redis_client.py`
- Test: `tests/services/cli/test_redis_client.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/cli/test_redis_client.py
from __future__ import annotations
import json
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from services.cli.redis_client import LabmateRedisClient


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.xadd = AsyncMock(return_value="msg-id")
    r.subscribe = AsyncMock()
    r.get = AsyncMock(return_value=json.dumps({"ok": True, "state": {"final_answer": "hello"}}).encode())
    return r


@pytest.mark.asyncio
async def test_push_task(mock_redis):
    client = LabmateRedisClient.__new__(LabmateRedisClient)
    client._redis = mock_redis
    await client.push_task(
        task_id="t-1",
        task="write hello world",
        session_id="s-1",
        user_id="u-1",
        workspace_id="ws-1",
    )
    mock_redis.xadd.assert_called_once()
    call_args = mock_redis.xadd.call_args
    payload = json.loads(call_args[0][1]["payload"])
    assert payload["task_id"] == "t-1"
    assert payload["user_id"] == "u-1"
    assert payload["workspace_id"] == "ws-1"


@pytest.mark.asyncio
async def test_get_result_ok(mock_redis):
    client = LabmateRedisClient.__new__(LabmateRedisClient)
    client._redis = mock_redis
    # simulate pubsub message then get
    pubsub = AsyncMock()
    pubsub.get_message = AsyncMock(return_value={"type": "message", "data": b"ready"})
    mock_redis.pubsub = MagicMock(return_value=pubsub)
    result = await client.get_result("t-1", timeout=5.0)
    assert result["ok"] is True
    assert result["state"]["final_answer"] == "hello"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/services/cli/test_redis_client.py -v
```

- [ ] **Step 3: Implement redis_client.py**

```python
# services/cli/redis_client.py
from __future__ import annotations
import asyncio
import json
import os
import time
import uuid
import redis.asyncio as aioredis

GOALS_STREAM = "labmate:goals"
RESULT_PREFIX = "labmate:result:"


class LabmateRedisClient:
    def __init__(self, redis_url: str | None = None) -> None:
        url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self._redis = aioredis.from_url(url, decode_responses=False)

    async def push_task(
        self,
        task_id: str,
        task: str,
        session_id: str,
        user_id: str = "",
        workspace_id: str = "",
    ) -> None:
        payload = json.dumps({
            "task_id": task_id,
            "task": task,
            "session_id": session_id,
            "user_id": user_id,
            "workspace_id": workspace_id,
        })
        await self._redis.xadd(GOALS_STREAM, {"payload": payload})

    async def get_result(
        self,
        task_id: str,
        timeout: float = 300.0,
    ) -> dict:
        """Subscribe before push: subscribe, then wait for 'ready' pubsub, then GET."""
        key = f"{RESULT_PREFIX}{task_id}"
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(key)
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
                if msg and msg.get("type") == "message":
                    break
                await asyncio.sleep(0.1)
            else:
                return {"ok": False, "error": "timeout"}
        finally:
            await pubsub.unsubscribe(key)
            await pubsub.aclose()

        raw = await self._redis.get(key)
        if raw is None:
            return {"ok": False, "error": "result_missing"}
        return json.loads(raw)

    async def aclose(self) -> None:
        await self._redis.aclose()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/services/cli/test_redis_client.py -v
```
Expected: 2 tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add services/cli/redis_client.py tests/services/cli/test_redis_client.py
git commit -m "feat(cli): Redis client for push-to-goals-stream + pubsub result retrieval"
```

---

### Task 5: Renderer — Rich output

**Files:**
- Create: `services/cli/renderer.py`
- Test: `tests/services/cli/test_renderer.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/cli/test_renderer.py
from __future__ import annotations
import pytest
from io import StringIO
from unittest.mock import patch, MagicMock
from services.cli.renderer import Renderer, extract_answer


def test_extract_answer_final():
    state = {"final_answer": "The answer is 42."}
    assert extract_answer(state) == "The answer is 42."


def test_extract_answer_root_result():
    state = {"goal_tree": {"root": {"result": "done"}}}
    assert extract_answer(state) == "done"


def test_extract_answer_fallback():
    state = {}
    result = extract_answer(state)
    assert isinstance(result, str)


def test_renderer_instantiates():
    r = Renderer()
    assert r is not None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/services/cli/test_renderer.py -v
```

- [ ] **Step 3: Implement renderer.py**

```python
# services/cli/renderer.py
from __future__ import annotations
import json
from contextlib import contextmanager
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.spinner import Spinner
from rich.live import Live
from rich.text import Text


def extract_answer(state: Any) -> str:
    if isinstance(state, dict):
        if state.get("final_answer"):
            return state["final_answer"]
        root = state.get("goal_tree", {}).get("root", {})
        if root.get("result"):
            return root["result"]
        # last-resort: truncated JSON
        raw = json.dumps(state, default=str)
        return raw[:3000] + ("…" if len(raw) > 3000 else "")
    return str(state)


class Renderer:
    """Wraps Rich console for Labmate CLI output."""

    def __init__(self) -> None:
        self._console = Console(highlight=False)

    def print_answer(self, text: str, session_id: str = "") -> None:
        self._console.print()
        self._console.print(Markdown(text))
        if session_id:
            self._console.print(
                f"\n[dim]session: {session_id}[/dim]",
                highlight=False,
            )

    def print_error(self, message: str) -> None:
        self._console.print(f"[bold red]Error:[/bold red] {message}")

    def print_info(self, message: str) -> None:
        self._console.print(f"[dim]{message}[/dim]")

    def print_workspace(self, name: str, workspace_id: str) -> None:
        self._console.print(
            f"[bold cyan]Workspace:[/bold cyan] {name}  "
            f"[dim]({workspace_id[:8]}…)[/dim]"
        )

    @contextmanager
    def thinking(self, label: str = "Thinking…"):
        """Context manager that shows a spinner while work is in flight."""
        with Live(
            Spinner("dots", text=Text(label, style="dim")),
            console=self._console,
            refresh_per_second=10,
            transient=True,
        ):
            yield
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/services/cli/test_renderer.py -v
```
Expected: 4 tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add services/cli/renderer.py tests/services/cli/test_renderer.py
git commit -m "feat(cli): Rich renderer with Markdown output and thinking spinner"
```

---

### Task 6: Workspace picker

**Files:**
- Create: `services/cli/workspace_picker.py`

No isolated unit tests — depends on interactive input. Covered by integration test in Task 7.

- [ ] **Step 1: Implement workspace_picker.py**

```python
# services/cli/workspace_picker.py
"""Interactive workspace selection at CLI startup."""
from __future__ import annotations
import uuid
from dataclasses import dataclass

from rich.console import Console
from rich.table import Table
from rich.prompt import Prompt, IntPrompt

console = Console()


@dataclass
class WorkspaceChoice:
    workspace_id: str
    name: str
    paths: list[str]
    instructions: str


def pick_workspace(existing: list[dict]) -> WorkspaceChoice:
    """Present a workspace selection menu; return the chosen (or newly created) workspace."""
    if not existing:
        return _create_workspace()

    table = Table(title="Your Workspaces", show_lines=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Name", style="bold")
    table.add_column("Paths")
    table.add_column("ID", style="dim")

    for i, ws in enumerate(existing, 1):
        paths = ", ".join(ws.get("paths", [])) or "[dim]none[/dim]"
        table.add_row(str(i), ws["name"], paths, ws["workspace_id"][:8] + "…")

    table.add_row(
        str(len(existing) + 1),
        "[green]+ New workspace[/green]",
        "",
        "",
    )
    console.print(table)

    choice = IntPrompt.ask(
        "Select workspace",
        default=1,
        choices=[str(i) for i in range(1, len(existing) + 2)],
    )

    if choice == len(existing) + 1:
        return _create_workspace()

    ws = existing[choice - 1]
    return WorkspaceChoice(
        workspace_id=ws["workspace_id"],
        name=ws["name"],
        paths=ws.get("paths", []),
        instructions=ws.get("instructions", ""),
    )


def _create_workspace() -> WorkspaceChoice:
    name = Prompt.ask("Workspace name", default="my-lab")
    paths_raw = Prompt.ask(
        "Code directories (comma-separated, or leave blank)",
        default="",
    )
    paths = [p.strip() for p in paths_raw.split(",") if p.strip()]
    instructions = Prompt.ask(
        "Per-workspace instructions (or leave blank)",
        default="",
    )
    return WorkspaceChoice(
        workspace_id=str(uuid.uuid4()),
        name=name,
        paths=paths,
        instructions=instructions,
    )
```

- [ ] **Step 2: Commit**

```bash
git add services/cli/workspace_picker.py
git commit -m "feat(cli): interactive workspace selection and creation menu"
```

---

### Task 7: REPL loop

**Files:**
- Create: `services/cli/repl.py`

- [ ] **Step 1: Implement repl.py**

```python
# services/cli/repl.py
"""Main REPL loop — Claude Code-style interactive session."""
from __future__ import annotations
import asyncio
import subprocess
import uuid
from dataclasses import dataclass

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from pathlib import Path

from .identity import Identity
from .redis_client import LabmateRedisClient
from .renderer import Renderer, extract_answer
from .session_store import SessionStore, SessionRecord

HISTORY_PATH = Path.home() / ".labmate" / "input_history"

SLASH_COMMANDS = {
    "/help": "Show this help",
    "/sessions": "List recent sessions",
    "/workspace": "Show current workspace",
    "/quit": "Exit Labmate",
}

PROMPT_STYLE = Style.from_dict({"prompt": "bold cyan"})


@dataclass
class REPLContext:
    identity: Identity
    workspace_id: str
    workspace_name: str
    workspace_paths: list[str]
    workspace_instructions: str
    session_id: str
    redis_url: str


class REPL:
    def __init__(self, ctx: REPLContext) -> None:
        self._ctx = ctx
        self._renderer = Renderer()
        self._redis = LabmateRedisClient(ctx.redis_url)
        self._sessions = SessionStore()
        self._prompt_session = PromptSession(
            history=FileHistory(str(HISTORY_PATH)),
            style=PROMPT_STYLE,
        )

    async def run(self) -> None:
        self._renderer.print_workspace(self._ctx.workspace_name, self._ctx.workspace_id)
        self._renderer.print_info(
            f"Hi {self._ctx.identity.display_name}! "
            "Type your task, !<cmd> to run shell commands, or /help."
        )

        while True:
            try:
                raw = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._prompt_session.prompt("> "),
                )
            except (EOFError, KeyboardInterrupt):
                self._renderer.print_info("Goodbye.")
                break

            line = raw.strip()
            if not line:
                continue

            if line.startswith("!"):
                self._run_shell(line[1:].strip())
            elif line.startswith("/"):
                if not await self._handle_slash(line):
                    break
            else:
                await self._send_task(line)

        await self._redis.aclose()

    def _run_shell(self, cmd: str) -> None:
        """Run a shell command and print output — just like Claude Code's ! passthrough."""
        result = subprocess.run(cmd, shell=True, text=True, capture_output=True)
        if result.stdout:
            self._renderer._console.print(result.stdout, end="")
        if result.stderr:
            self._renderer._console.print(result.stderr, end="", style="dim red")

    async def _handle_slash(self, line: str) -> bool:
        """Return False to signal exit."""
        cmd = line.split()[0].lower()
        if cmd in ("/quit", "/exit", "/q"):
            self._renderer.print_info("Goodbye.")
            return False
        elif cmd == "/help":
            for c, desc in SLASH_COMMANDS.items():
                self._renderer._console.print(f"  [bold]{c}[/bold]  {desc}")
        elif cmd == "/workspace":
            self._renderer.print_workspace(self._ctx.workspace_name, self._ctx.workspace_id)
            if self._ctx.workspace_paths:
                self._renderer.print_info("Paths: " + ", ".join(self._ctx.workspace_paths))
        elif cmd == "/sessions":
            sessions = self._sessions.list(workspace_id=self._ctx.workspace_id, limit=10)
            if not sessions:
                self._renderer.print_info("No sessions yet.")
            for s in sessions:
                self._renderer._console.print(
                    f"  [dim]{s.created_at[:16]}[/dim]  {s.task_preview[:60]}"
                    f"  [dim]{s.session_id[:8]}…[/dim]"
                )
        else:
            self._renderer.print_error(f"Unknown command: {line}")
        return True

    async def _send_task(self, task: str) -> None:
        task_id = str(uuid.uuid4())
        self._sessions.append(SessionRecord(
            session_id=self._ctx.session_id,
            workspace_id=self._ctx.workspace_id,
            workspace_name=self._ctx.workspace_name,
            task_preview=task[:120],
        ))

        with self._renderer.thinking("Working…"):
            await self._redis.push_task(
                task_id=task_id,
                task=task,
                session_id=self._ctx.session_id,
                user_id=self._ctx.identity.user_id,
                workspace_id=self._ctx.workspace_id,
            )
            result = await self._redis.get_result(task_id, timeout=300.0)

        if not result.get("ok"):
            self._renderer.print_error(result.get("error", "Unknown error"))
            return

        answer = extract_answer(result.get("state", {}))
        self._renderer.print_answer(answer, session_id=self._ctx.session_id)
```

- [ ] **Step 2: Commit**

```bash
git add services/cli/repl.py
git commit -m "feat(cli): REPL loop with slash commands, shell passthrough, task dispatch"
```

---

### Task 8: Entry point (main.py)

**Files:**
- Create: `services/cli/main.py`

- [ ] **Step 1: Implement main.py**

```python
# services/cli/main.py
"""
Labmate CLI — interactive agent session.

Usage:
    python -m services.cli                   # start REPL with workspace picker
    python -m services.cli --resume s-abc    # resume a previous session
    python -m services.cli "do this task"    # one-shot (no REPL)

Env:
    REDIS_URL   redis://localhost:6379/0
"""
from __future__ import annotations
import asyncio
import os
import sys
import uuid
from pathlib import Path
from typing import Optional

import typer

from .identity import load_or_create_identity
from .redis_client import LabmateRedisClient
from .renderer import Renderer, extract_answer
from .repl import REPL, REPLContext
from .session_store import SessionStore
from .workspace_picker import pick_workspace

app = typer.Typer(add_completion=False, help="Labmate — autonomous agent CLI")
_renderer = Renderer()


def _redis_url() -> str:
    return os.getenv("REDIS_URL", "redis://localhost:6379/0")


def _resolve_workspace(identity_user_id: str) -> dict:
    """Fetch workspace list from Redis (via orchestrator HTTP) or fallback to local cache.

    For now: use a local JSON cache at ~/.labmate/workspaces.json that the CLI
    writes itself when creating workspaces. Future: query orchestrator REST API.
    """
    ws_cache = Path.home() / ".labmate" / "workspaces.json"
    existing: list[dict] = []
    if ws_cache.exists():
        import json
        try:
            all_ws = json.loads(ws_cache.read_text())
            existing = [w for w in all_ws if w.get("user_id") == identity_user_id]
        except Exception:
            existing = []
    return existing, ws_cache


def _save_workspace(ws_cache: Path, workspace: dict) -> None:
    import json
    existing = []
    if ws_cache.exists():
        try:
            existing = json.loads(ws_cache.read_text())
        except Exception:
            existing = []
    existing.append(workspace)
    ws_cache.parent.mkdir(parents=True, exist_ok=True)
    ws_cache.write_text(json.dumps(existing, indent=2))


@app.command()
def main(
    prompt: Optional[str] = typer.Argument(None, help="One-shot task (skips REPL)"),
    resume: Optional[str] = typer.Option(None, "--resume", "-r", help="Resume session ID"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Workspace ID to use"),
) -> None:
    asyncio.run(_async_main(prompt, resume, workspace))


async def _async_main(
    one_shot: str | None,
    resume_id: str | None,
    workspace_id_flag: str | None,
) -> None:
    identity = load_or_create_identity()
    existing_ws, ws_cache = _resolve_workspace(identity.user_id)

    if workspace_id_flag:
        match = next((w for w in existing_ws if w["workspace_id"] == workspace_id_flag), None)
        if match:
            ws_choice_data = match
        else:
            _renderer.print_error(f"Workspace {workspace_id_flag} not found.")
            raise SystemExit(1)
        from .workspace_picker import WorkspaceChoice
        ws_choice = WorkspaceChoice(**{k: ws_choice_data[k] for k in ["workspace_id","name","paths","instructions"]})
    else:
        ws_choice = pick_workspace(existing_ws)
        # persist newly created workspace
        _save_workspace(ws_cache, {
            "workspace_id": ws_choice.workspace_id,
            "name": ws_choice.name,
            "paths": ws_choice.paths,
            "instructions": ws_choice.instructions,
            "user_id": identity.user_id,
        })

    session_id = resume_id or str(uuid.uuid4())
    redis_url  = _redis_url()

    if one_shot:
        # Non-interactive: push one task and print result
        client = LabmateRedisClient(redis_url)
        task_id = str(uuid.uuid4())
        _renderer.print_workspace(ws_choice.name, ws_choice.workspace_id)
        with _renderer.thinking("Working…"):
            await client.push_task(
                task_id=task_id,
                task=one_shot,
                session_id=session_id,
                user_id=identity.user_id,
                workspace_id=ws_choice.workspace_id,
            )
            result = await client.get_result(task_id, timeout=300.0)
        await client.aclose()

        if not result.get("ok"):
            _renderer.print_error(result.get("error", "unknown"))
            raise SystemExit(1)
        _renderer.print_answer(extract_answer(result.get("state", {})), session_id=session_id)
        return

    # Interactive REPL
    ctx = REPLContext(
        identity=identity,
        workspace_id=ws_choice.workspace_id,
        workspace_name=ws_choice.name,
        workspace_paths=ws_choice.paths,
        workspace_instructions=ws_choice.instructions,
        session_id=session_id,
        redis_url=redis_url,
    )
    await REPL(ctx).run()


if __name__ == "__main__":
    app()
```

- [ ] **Step 2: Test the entry point**

```bash
# Verify it imports without error
cd /Users/zachstallbohm/Work/Labmate
python -m services.cli --help
```
Expected: shows help text with `prompt`, `--resume`, `--workspace` options.

- [ ] **Step 3: Commit**

```bash
git add services/cli/main.py
git commit -m "feat(cli): main entry point with one-shot, resume, and interactive REPL modes"
```

---

### Task 9: Integration smoke test

**Files:**
- Create: `tests/services/cli/test_integration_smoke.py`

This test verifies the full round-trip with mocked Redis (no live orchestrator needed).

- [ ] **Step 1: Write the smoke test**

```python
# tests/services/cli/test_integration_smoke.py
"""Smoke test: push a task, get a result, render it. No live Redis."""
from __future__ import annotations
import json
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from services.cli.redis_client import LabmateRedisClient
from services.cli.renderer import extract_answer, Renderer


@pytest.mark.asyncio
async def test_push_then_get_result():
    """Full push → subscribe → get cycle with mock Redis."""
    mock_redis = AsyncMock()
    mock_redis.xadd = AsyncMock(return_value="msg-1")
    mock_redis.get = AsyncMock(return_value=json.dumps({
        "ok": True,
        "state": {"final_answer": "42"}
    }).encode())

    pubsub = AsyncMock()
    pubsub.get_message = AsyncMock(return_value={"type": "message", "data": b"ready"})
    pubsub.aclose = AsyncMock()
    mock_redis.pubsub = MagicMock(return_value=pubsub)

    client = LabmateRedisClient.__new__(LabmateRedisClient)
    client._redis = mock_redis

    await client.push_task("t-1", "what is the answer?", "s-1", "u-1", "ws-1")
    result = await client.get_result("t-1", timeout=5.0)
    assert result["ok"] is True
    assert extract_answer(result["state"]) == "42"


def test_renderer_does_not_raise():
    r = Renderer()
    r.print_answer("# Hello\n\nThis is a **test**.", session_id="s-test")
    r.print_error("something went wrong")
    r.print_info("info message")
```

- [ ] **Step 2: Run the smoke test**

```bash
python -m pytest tests/services/cli/test_integration_smoke.py -v
```
Expected: 2 tests PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/services/cli/test_integration_smoke.py
git commit -m "test(cli): integration smoke test for push/get/render cycle"
```
