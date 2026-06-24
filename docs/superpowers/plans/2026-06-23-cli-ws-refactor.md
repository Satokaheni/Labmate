# CLI WebSocket Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the CLI's direct Redis dependency with a WebSocket connection to the ws_gateway, so the CLI authenticates with JWT and routes all traffic (task submission, event streaming, tool results) through the gateway — exactly like the Electron frontend.

**Architecture:** A new `LabmateWSClient` wraps a `websockets` connection to the ws_gateway. It performs a JWT login via HTTP POST to `/auth/login` before connecting, then sends the token in the WS auth handshake. `WSEventStream` reads WS frames, normalises them from the gateway's camelCase format into the CLI's internal snake_case format (so `StreamRenderer` needs no changes), and accumulates them for result synthesis. `_ToolInterceptingStream` is updated to use a `send_result` callback (instead of writing to Redis directly), which the WS client implements by sending a `{type: 'tool.result'}` frame over the socket.

**Tech Stack:** Python `websockets>=12.0` (async WS), stdlib `urllib.request` + `asyncio.to_thread` for the login HTTP call, `argon2-cffi` stays on the server side only. `redis>=5.0,<6` stays in requirements.txt (still imported by `event_stream.py` and `redis_client.py` for backward-compat; only the active CLI code path stops using Redis).

**CRITICAL SECURITY CONSTRAINT:** Discord connector is deferred — do NOT wire, import, or reference it in any active code path. Lives in `services/connectors/deferred/`.

---

## Files

| File | Action |
|---|---|
| `services/cli/token_store.py` | Create — load/save/clear JWT from `~/.labmate/token.json` |
| `services/cli/ws_client.py` | Create — `LabmateWSClient` + `WSEventStream` + `_normalize_ws_event` |
| `services/cli/event_stream.py` | Modify — `_ToolInterceptingStream` takes callback; remove Redis import from interceptor; `run_task_with_streaming` drops `redis` param |
| `services/cli/local_tool_executor.py` | Modify — remove `handle_tool_request` (Redis-dependent) and its `aioredis` import |
| `services/cli/repl.py` | Modify — `REPLContext.redis_url` → `ws_url`; use `LabmateWSClient` |
| `services/cli/main.py` | Modify — read `LABMATE_GATEWAY_URL`; login flow; create `LabmateWSClient` |
| `services/cli/requirements.txt` | Modify — add `websockets>=12.0` |
| `tests/services/cli/test_token_store.py` | Create |
| `tests/services/cli/test_ws_client.py` | Create |
| `tests/services/cli/test_event_stream.py` | Modify — update `_ToolInterceptingStream` test for callback |
| `tests/services/cli/test_local_tool_executor.py` | Modify — remove `handle_tool_request` tests |
| `tests/services/cli/test_repl_streaming.py` | Modify — `redis_url` → `ws_url` in `REPLContext` |

---

### Task 1: `token_store.py` — cache JWT in `~/.labmate/token.json`

**Files:**
- Create: `services/cli/token_store.py`
- Create: `tests/services/cli/test_token_store.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/services/cli/test_token_store.py
from __future__ import annotations
import json
import time
from pathlib import Path
import pytest
from services.cli.token_store import load_token, save_token, clear_token, _decode_exp


def _make_token(exp_offset: int) -> str:
    """Build a minimal JWT with a real exp claim (no signature needed for local decode)."""
    import base64
    payload = json.dumps({"sub": "u-1", "exp": int(time.time()) + exp_offset}).encode()
    b64 = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    return f"header.{b64}.sig"


def test_save_and_load_token(tmp_path: Path):
    token_path = tmp_path / "token.json"
    tok = _make_token(3600)
    save_token(tok, path=token_path)
    assert load_token(path=token_path) == tok


def test_load_returns_none_when_file_missing(tmp_path: Path):
    assert load_token(path=tmp_path / "missing.json") is None


def test_load_returns_none_when_expired(tmp_path: Path):
    token_path = tmp_path / "token.json"
    tok = _make_token(-10)   # expired 10 s ago
    save_token(tok, path=token_path)
    assert load_token(path=token_path) is None


def test_clear_token_removes_file(tmp_path: Path):
    token_path = tmp_path / "token.json"
    save_token(_make_token(3600), path=token_path)
    clear_token(path=token_path)
    assert not token_path.exists()


def test_clear_token_is_idempotent(tmp_path: Path):
    clear_token(path=tmp_path / "missing.json")  # must not raise


def test_decode_exp_returns_none_on_garbage():
    assert _decode_exp("not.a.jwt") is None
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/cli/test_token_store.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'services.cli.token_store'`

- [ ] **Step 3: Create `services/cli/token_store.py`**

```python
# services/cli/token_store.py
"""Cache the ws_gateway JWT in ~/.labmate/token.json.

The token is stored as plain text. On load, the exp claim is decoded (no
signature check — the server verifies on connect) to skip re-login when the
token is still valid.
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Optional

TOKEN_PATH = Path.home() / ".labmate" / "token.json"


def _decode_exp(token: str) -> Optional[int]:
    """Return the exp claim from a JWT payload, or None on any decode error."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        padding = 4 - len(parts[1]) % 4
        payload = base64.urlsafe_b64decode(parts[1] + "=" * padding)
        return int(json.loads(payload).get("exp", 0))
    except Exception:
        return None


def load_token(*, path: Path = TOKEN_PATH) -> Optional[str]:
    """Return cached token if the file exists and the token has not expired."""
    if not path.exists():
        return None
    try:
        token = path.read_text().strip()
    except OSError:
        return None
    exp = _decode_exp(token)
    if exp is None or exp <= int(time.time()):
        return None
    return token


def save_token(token: str, *, path: Path = TOKEN_PATH) -> None:
    """Write token to disk (creates parent dirs as needed)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token)


def clear_token(*, path: Path = TOKEN_PATH) -> None:
    """Delete the cached token file (idempotent)."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
```

- [ ] **Step 4: Run tests — confirm all 6 pass**

```bash
cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/cli/test_token_store.py -v 2>&1 | tail -10
```

Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add services/cli/token_store.py tests/services/cli/test_token_store.py
git commit -m "feat(cli): JWT token cache in ~/.labmate/token.json"
```

---

### Task 2: `ws_client.py` — `LabmateWSClient` + `WSEventStream`

**Files:**
- Create: `services/cli/ws_client.py`
- Create: `tests/services/cli/test_ws_client.py`

This is the core of the refactor. `LabmateWSClient` implements the same external interface as `LabmateRedisClient` (`push_task`, `subscribe_events`, `get_result`, `aclose`) so `run_task_with_streaming` needs no changes. It also exposes `send_tool_result` for the `_ToolInterceptingStream` callback.

`WSEventStream` wraps the raw WS frames: `first(timeout)` / `events()` / `aclose()`. Each event is passed through `_normalize_ws_event` which converts the gateway's camelCase format back to the CLI's snake_case format so `StreamRenderer` continues to work unchanged.

**Event normalisation map (ws_gateway → CLI internal):**

| WS frame type | CLI type after normalise | Notes |
|---|---|---|
| `node.enter` | `turn.start` | `thinkingBudget` → `thinking_budget` |
| `reasoning.delta` | `reasoning` | same `text` field |
| `tool.start` | `tool.start` | `toolCall.{id,reasoningWhy,…}` → `tool_id`, `reasoning_why`, … |
| `tool.done` | `tool.done` | `toolId` → `tool_id`, `durationMs` → `duration_ms` |
| `answer.delta` | `answer.delta` | unchanged |
| `turn.done` | `turn.done` | unchanged |
| `tool.request` | `tool.request` | `toolRequestId` → `tool_request_id`; pass through |
| everything else | pass through | auth frames, turn.created, etc. |

- [ ] **Step 1: Add `websockets>=12.0` to requirements and install**

```bash
cd /Users/zachstallbohm/Work/Labmate/services/cli && echo 'websockets>=12.0' >> requirements.txt && pip install websockets 2>&1 | tail -3
```

- [ ] **Step 2: Write failing tests**

```python
# tests/services/cli/test_ws_client.py
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.cli.ws_client import LabmateWSClient, WSEventStream, _normalize_ws_event


# ── _normalize_ws_event ────────────────────────────────────────────────────────

def test_normalize_node_enter():
    ev = {"type": "node.enter", "turnId": "t-1", "node": "plan", "thinkingBudget": 5000}
    out = _normalize_ws_event(ev)
    assert out == {"type": "turn.start", "node": "plan", "thinking_budget": 5000}


def test_normalize_reasoning_delta():
    out = _normalize_ws_event({"type": "reasoning.delta", "turnId": "t-1", "text": "hmm"})
    assert out == {"type": "reasoning", "text": "hmm"}


def test_normalize_tool_start():
    ev = {
        "type": "tool.start",
        "turnId": "t-1",
        "toolCall": {"id": "tc-1", "name": "bash", "kind": "tool",
                     "summary": "sum", "reasoningWhy": "why", "args": {"cmd": "ls"}},
    }
    out = _normalize_ws_event(ev)
    assert out == {
        "type": "tool.start",
        "tool_id": "tc-1",
        "name": "bash",
        "kind": "tool",
        "summary": "sum",
        "reasoning_why": "why",
        "args": {"cmd": "ls"},
    }


def test_normalize_tool_done():
    ev = {"type": "tool.done", "turnId": "t-1", "toolId": "tc-1",
          "status": "done", "summary": "ok", "result": None, "durationMs": 123}
    out = _normalize_ws_event(ev)
    assert out == {
        "type": "tool.done",
        "tool_id": "tc-1",
        "status": "done",
        "summary": "ok",
        "result": None,
        "duration_ms": 123,
    }


def test_normalize_tool_request():
    ev = {"type": "tool.request", "turnId": "t-1",
          "toolRequestId": "req-1", "name": "read_file", "args": {"path": "a.txt"}}
    out = _normalize_ws_event(ev)
    assert out == {
        "type": "tool.request",
        "tool_request_id": "req-1",
        "name": "read_file",
        "args": {"path": "a.txt"},
    }


def test_normalize_passthrough():
    ev = {"type": "answer.delta", "turnId": "t-1", "text": "hi"}
    assert _normalize_ws_event(ev) == ev


# ── WSEventStream ──────────────────────────────────────────────────────────────

def _make_ws(*events: dict):
    """Fake websocket that serves events as JSON strings then raises StopAsyncIteration."""
    frames = [json.dumps(e) for e in events]
    ws = MagicMock()
    ws.recv = AsyncMock(side_effect=frames + [Exception("closed")])
    ws.send = AsyncMock()
    return ws


@pytest.mark.asyncio
async def test_ws_event_stream_first_returns_first_event():
    ws = _make_ws(
        {"type": "turn.created"},
        {"type": "node.enter", "turnId": "t-1", "node": "plan", "thinkingBudget": 0},
        {"type": "turn.done", "turnId": "t-1", "status": "complete"},
    )
    stream = WSEventStream(ws)
    first = await stream.first(timeout=1.0)
    assert first is not None
    assert first["type"] == "turn.created"


@pytest.mark.asyncio
async def test_ws_event_stream_events_normalises_and_stops_at_turn_done():
    ws = _make_ws(
        {"type": "node.enter", "turnId": "t-1", "node": "plan", "thinkingBudget": 0},
        {"type": "answer.delta", "turnId": "t-1", "text": "hello"},
        {"type": "turn.done", "turnId": "t-1", "status": "complete"},
        {"type": "answer.delta", "turnId": "t-1", "text": "AFTER"},  # must not appear
    )
    stream = WSEventStream(ws)
    _ = await stream.first(timeout=1.0)
    seen = [ev async for ev in stream.events()]
    assert [e["type"] for e in seen] == ["turn.start", "answer.delta", "turn.done"]
    assert seen[1]["text"] == "hello"


@pytest.mark.asyncio
async def test_ws_event_stream_result_synthesises_answer():
    ws = _make_ws(
        {"type": "node.enter", "turnId": "t-1", "node": "plan", "thinkingBudget": 0},
        {"type": "answer.delta", "turnId": "t-1", "text": "hello "},
        {"type": "answer.delta", "turnId": "t-1", "text": "world"},
        {"type": "turn.done", "turnId": "t-1", "status": "complete"},
    )
    stream = WSEventStream(ws)
    _ = await stream.first(timeout=1.0)
    async for _ in stream.events():
        pass
    result = stream.result()
    assert result["ok"] is True
    assert result["state"]["final_answer"] == "hello world"


@pytest.mark.asyncio
async def test_ws_event_stream_result_error_on_turn_done_error():
    ws = _make_ws(
        {"type": "turn.done", "turnId": "t-1", "status": "error"},
    )
    stream = WSEventStream(ws)
    _ = await stream.first(timeout=1.0)
    async for _ in stream.events():
        pass
    assert stream.result()["ok"] is False


# ── LabmateWSClient ────────────────────────────────────────────────────────────

def _make_client_ws(*events: dict):
    """Fake WS that serves auth handshake then events."""
    # First recv returns auth.ok, subsequent calls return the events
    frames = [json.dumps({"type": "auth.ok", "user": {"id": "u-1", "email": "a@b.com", "role": "user"}})]
    frames += [json.dumps(e) for e in events]
    ws = MagicMock()
    ws.recv = AsyncMock(side_effect=frames + [Exception("closed")])
    ws.send = AsyncMock()
    ws.close = AsyncMock()
    ws.__aenter__ = AsyncMock(return_value=ws)
    ws.__aexit__ = AsyncMock(return_value=False)
    return ws


@pytest.mark.asyncio
async def test_client_push_task_sends_send_frame():
    ws = _make_client_ws(
        {"type": "turn.created", "turn": {"id": "turn-1"}},
        {"type": "turn.done", "turnId": "turn-1", "status": "complete"},
    )
    with patch("services.cli.ws_client.websockets.connect", new=AsyncMock(return_value=ws)):
        client = LabmateWSClient("ws://localhost:8787/ws", "tok")
        await client.connect()
        await client.push_task("task-1", "do the thing", "session-1")
        sent = [json.loads(c.args[0]) for c in ws.send.call_args_list]
        assert any(f.get("type") == "send" and f.get("text") == "do the thing" for f in sent)


@pytest.mark.asyncio
async def test_client_send_tool_result_sends_tool_result_frame():
    ws = _make_client_ws()
    with patch("services.cli.ws_client.websockets.connect", new=AsyncMock(return_value=ws)):
        client = LabmateWSClient("ws://localhost:8787/ws", "tok")
        await client.connect()
        await client.send_tool_result("req-1", {"content": "hi"}, None)
        sent = [json.loads(c.args[0]) for c in ws.send.call_args_list]
        assert any(
            f.get("type") == "tool.result"
            and f.get("toolRequestId") == "req-1"
            and f.get("result") == {"content": "hi"}
            for f in sent
        )


@pytest.mark.asyncio
async def test_client_get_result_consumes_events_and_returns_answer():
    ws = _make_client_ws(
        {"type": "turn.created"},
        {"type": "answer.delta", "turnId": "t-1", "text": "done"},
        {"type": "turn.done", "turnId": "t-1", "status": "complete"},
    )
    with patch("services.cli.ws_client.websockets.connect", new=AsyncMock(return_value=ws)):
        client = LabmateWSClient("ws://localhost:8787/ws", "tok")
        await client.connect()
        await client.push_task("task-1", "go", "s-1")
        result = await client.get_result("task-1", timeout=5.0)
    assert result["ok"] is True
    assert result["state"]["final_answer"] == "done"
```

- [ ] **Step 3: Run to confirm failure**

```bash
cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/cli/test_ws_client.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'services.cli.ws_client'`

- [ ] **Step 4: Create `services/cli/ws_client.py`**

```python
# services/cli/ws_client.py
"""WebSocket-backed client for the Labmate ws_gateway.

Replaces LabmateRedisClient — implements the same interface
(push_task / subscribe_events / get_result / aclose) plus
send_tool_result, so the active CLI code path needs no Redis access.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import urllib.request
from typing import Any, AsyncIterator, Optional

import websockets
import websockets.exceptions


# ── Event normalisation ────────────────────────────────────────────────────────

def _normalize_ws_event(ev: dict) -> dict:
    """Convert a ws_gateway camelCase StreamEvent to CLI snake_case format.

    The CLI's StreamRenderer was written against the raw orchestrator event
    format. This adapter keeps StreamRenderer unchanged.
    """
    etype = ev.get("type")

    if etype == "node.enter":
        return {
            "type": "turn.start",
            "node": ev.get("node", ""),
            "thinking_budget": ev.get("thinkingBudget", 0),
        }

    if etype == "reasoning.delta":
        return {"type": "reasoning", "text": ev.get("text", "")}

    if etype == "tool.start":
        tc = ev.get("toolCall", {})
        return {
            "type": "tool.start",
            "tool_id": tc.get("id", ""),
            "name": tc.get("name", ""),
            "kind": tc.get("kind", "tool"),
            "summary": tc.get("summary", ""),
            "reasoning_why": tc.get("reasoningWhy", ""),
            "args": tc.get("args", {}),
        }

    if etype == "tool.done":
        return {
            "type": "tool.done",
            "tool_id": ev.get("toolId", ""),
            "status": ev.get("status", "done"),
            "summary": ev.get("summary", ""),
            "result": ev.get("result"),
            "duration_ms": ev.get("durationMs", 0),
        }

    if etype == "tool.request":
        return {
            "type": "tool.request",
            "tool_request_id": ev.get("toolRequestId", ""),
            "name": ev.get("name", ""),
            "args": ev.get("args", {}),
        }

    # answer.delta, turn.done, turn.created, auth.ok, boot.*, cancel — pass through
    return ev


# ── WSEventStream ──────────────────────────────────────────────────────────────

class WSEventStream:
    """Read-once stream of normalised events from the ws_gateway for one turn."""

    def __init__(self, ws) -> None:
        self._ws = ws
        self._pending: Optional[dict] = None  # first() buffer
        self._done: bool = False
        self._events: list[dict] = []

    async def _recv(self) -> Optional[dict]:
        try:
            raw = await self._ws.recv()
            ev = json.loads(raw)
            return _normalize_ws_event(ev)
        except (websockets.exceptions.ConnectionClosed, json.JSONDecodeError, Exception):
            return None

    async def first(self, timeout: float) -> Optional[dict]:
        """Return the first event within timeout seconds, or None.

        The returned event is buffered and replayed as the first item of events().
        """
        try:
            ev = await asyncio.wait_for(self._recv(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        if ev is None:
            return None
        self._events.append(ev)
        self._pending = ev
        return ev

    async def events(self) -> AsyncIterator[dict]:
        """Yield normalised events until (and including) turn.done, then stop."""
        if self._pending is not None:
            ev, self._pending = self._pending, None
            yield ev
            if ev.get("type") == "turn.done":
                self._done = True
                return
        while not self._done:
            ev = await self._recv()
            if ev is None:
                self._done = True
                return
            self._events.append(ev)
            yield ev
            if ev.get("type") == "turn.done":
                self._done = True
                return

    async def aclose(self) -> None:
        pass  # WS connection belongs to LabmateWSClient; don't close it here

    def result(self) -> dict:
        """Synthesise a result dict from accumulated events."""
        answer = ""
        clarification = False
        clarification_q = ""
        status = "complete"
        for ev in self._events:
            etype = ev.get("type")
            if etype == "answer.delta":
                answer += ev.get("text", "")
            elif etype == "clarification_request":
                clarification = True
                clarification_q = ev.get("question", "")
            elif etype == "turn.done":
                status = ev.get("status", "complete")
        return {
            "ok": status != "error",
            "state": {
                "final_answer": answer,
                "awaiting_clarification": clarification,
                "clarification_question": clarification_q,
            },
        }


# ── LabmateWSClient ────────────────────────────────────────────────────────────

def _ws_to_http(ws_url: str) -> str:
    """Convert ws:// or wss:// URL to http:// or https:// for REST calls."""
    return ws_url.replace("wss://", "https://").replace("ws://", "http://").removesuffix("/ws")


async def _http_post_json(url: str, body: dict) -> dict:
    """POST JSON synchronously in a thread (no extra deps)."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    def _do() -> dict:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    return await asyncio.to_thread(_do)


class LabmateWSClient:
    """WebSocket-backed client for the ws_gateway.

    Drop-in replacement for LabmateRedisClient: same push_task /
    subscribe_events / get_result / aclose interface, plus send_tool_result
    for the _ToolInterceptingStream callback.

    Usage:
        client = LabmateWSClient(ws_url, token)
        await client.connect()
        ...
        await client.aclose()
    """

    def __init__(self, ws_url: str, token: str) -> None:
        self._ws_url = ws_url
        self._token = token
        self._ws: Any = None
        self._current_stream: Optional[WSEventStream] = None

    @classmethod
    async def login(cls, ws_url: str, email: str, password: str) -> str:
        """POST /auth/login and return the JWT token."""
        base = _ws_to_http(ws_url)
        resp = await _http_post_json(f"{base}/auth/login", {"email": email, "password": password})
        return resp["token"]

    async def connect(self) -> None:
        """Open the WebSocket and perform the auth handshake."""
        self._ws = await websockets.connect(self._ws_url)
        await self._ws.send(json.dumps({"type": "auth", "token": self._token}))
        raw = await self._ws.recv()
        frame = json.loads(raw)
        if frame.get("type") == "auth.error":
            await self._ws.close()
            raise PermissionError(f"ws_gateway auth rejected: {frame.get('reason', 'unknown')}")
        # auth.ok — connection is ready

    async def push_task(
        self,
        task_id: str,
        task: str,
        session_id: str,
        user_id: str = "",
        workspace_id: str = "",
    ) -> None:
        """Send a task to the orchestrator via ws_gateway."""
        self._current_stream = WSEventStream(self._ws)
        await self._ws.send(json.dumps({
            "type": "send",
            "text": task,
            "sessionId": session_id,
        }))

    def subscribe_events(self, task_id: str) -> WSEventStream:
        """Return the WSEventStream for the current turn (task_id ignored)."""
        if self._current_stream is None:
            raise RuntimeError("subscribe_events called before push_task")
        return self._current_stream

    async def get_result(self, task_id: str, timeout: float = 300.0) -> dict:
        """Wait for turn.done and return a synthesised result dict."""
        stream = self._current_stream
        if stream is None:
            return {"ok": False, "error": "no_active_turn"}
        if not stream._done:
            try:
                async with asyncio.timeout(timeout):
                    async for _ in stream.events():
                        pass
            except asyncio.TimeoutError:
                return {"ok": False, "error": "timeout"}
        return stream.result()

    async def send_tool_result(
        self,
        tool_request_id: str,
        result: Any,
        error: Optional[str],
    ) -> None:
        """Send a tool.result frame back to the ws_gateway."""
        await self._ws.send(json.dumps({
            "type": "tool.result",
            "toolRequestId": tool_request_id,
            "result": result,
            "error": error,
        }))

    async def aclose(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
```

- [ ] **Step 5: Run tests — confirm all pass**

```bash
cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/cli/test_ws_client.py -v 2>&1 | tail -15
```

Expected: `13 passed` (6 normalise + 4 stream + 3 client)

If `asyncio.timeout` isn't available (Python < 3.11), replace with `asyncio.wait_for(...)`.

- [ ] **Step 6: Commit**

```bash
git add services/cli/ws_client.py services/cli/requirements.txt tests/services/cli/test_ws_client.py
git commit -m "feat(cli): LabmateWSClient + WSEventStream — WS-backed replacement for Redis client"
```

---

### Task 3: Update `_ToolInterceptingStream` + clean up `local_tool_executor.py`

**Files:**
- Modify: `services/cli/event_stream.py`
- Modify: `services/cli/local_tool_executor.py`
- Modify: `tests/services/cli/test_event_stream.py`
- Modify: `tests/services/cli/test_local_tool_executor.py`

`_ToolInterceptingStream` currently takes a `redis` client and calls `handle_tool_request(redis, ev, workspace=workspace)`. After this task it takes a `send_result` async callback with signature `(tool_request_id: str, result, error: str | None) -> None` and calls `execute_local_tool` directly.

`handle_tool_request` in `local_tool_executor.py` is the only thing that imports `redis.asyncio`; deleting it removes that import.

`run_task_with_streaming` drops its `redis` parameter (never used externally) and passes `client.send_tool_result` as the callback when `workspace` is provided.

- [ ] **Step 1: Update the test for `_ToolInterceptingStream` in `test_event_stream.py`**

Read `tests/services/cli/test_event_stream.py`. The test `test_tool_intercepting_stream_handles_tool_request` is in `test_local_tool_executor.py` (not `test_event_stream.py`). Update that test — see Step 2.

The `test_event_stream.py` tests only test `tail_events` and `EventStream` — those stay unchanged. No edit needed to `test_event_stream.py`.

- [ ] **Step 2: Update `test_local_tool_executor.py`**

Remove the two `handle_tool_request` tests and the `_ToolInterceptingStream` test that uses Redis. Replace with a callback-based `_ToolInterceptingStream` test.

In `tests/services/cli/test_local_tool_executor.py`, delete these three tests entirely:
- `test_handle_tool_request_writes_result`
- `test_handle_tool_request_reports_error`
- `test_tool_intercepting_stream_handles_tool_request`

And the `redis` fixture. Replace with:

```python
# tests/services/cli/test_local_tool_executor.py
# (keep all existing test_read_file / test_write_file / test_list_dir /
# test_path_escape / test_absolute_path_escape / test_unknown_tool tests;
# add only this new test at the bottom)

@pytest.mark.asyncio
async def test_tool_intercepting_stream_calls_send_result_callback(tmp_path: Path):
    """_ToolInterceptingStream calls the send_result callback and yields all events."""
    from pathlib import Path as P
    (tmp_path / "readme.txt").write_text("content", encoding="utf-8")

    events_list = [
        {"type": "turn.start", "seq": 0},
        {
            "type": "tool.request",
            "tool_request_id": "req-cb",
            "name": "read_file",
            "args": {"path": "readme.txt"},
        },
        {"type": "turn.done", "status": "complete", "seq": 2},
    ]

    async def _fake_gen(evs):
        for e in evs:
            yield e

    from unittest.mock import AsyncMock, patch
    from services.cli.event_stream import EventStream, _ToolInterceptingStream

    calls: list[tuple] = []
    async def fake_send_result(tool_request_id, result, error):
        calls.append((tool_request_id, result, error))

    with patch("services.cli.event_stream.tail_events", return_value=_fake_gen(events_list)):
        stream = EventStream("redis://x", "t-x")
        _ = await stream.first(timeout=1.0)

        interceptor = _ToolInterceptingStream(stream, fake_send_result, str(tmp_path))
        seen = []
        async for ev in interceptor.events():
            seen.append(ev["type"])

    assert seen == ["turn.start", "tool.request", "turn.done"]
    assert len(calls) == 1
    assert calls[0] == ("req-cb", {"content": "content"}, None)
```

- [ ] **Step 3: Run to confirm the new test FAILS (function signature not yet updated)**

```bash
cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/cli/test_local_tool_executor.py -v 2>&1 | tail -15
```

Expected: error because `_ToolInterceptingStream` still takes `redis` not `send_result`.

- [ ] **Step 4: Update `services/cli/event_stream.py`**

Replace `_ToolInterceptingStream` and `run_task_with_streaming`:

```python
class _ToolInterceptingStream:
    """Wraps an EventStream to execute tool.request events against local disk.

    send_result: async callable(tool_request_id, result, error) → None
    Used by LabmateWSClient (sends tool.result over WS) or any other transport.
    """

    def __init__(self, stream, send_result, workspace: str) -> None:
        self._stream = stream
        self._send_result = send_result
        self._workspace = workspace

    async def events(self):
        from services.cli.local_tool_executor import execute_local_tool
        async for ev in self._stream.events():
            if ev.get("type") == "tool.request":
                tool_request_id = ev.get("tool_request_id", "")
                name = ev.get("name", "")
                args = ev.get("args", {}) or {}
                result = None
                error = None
                try:
                    result = execute_local_tool(name, args, workspace=self._workspace)
                except Exception as exc:
                    error = str(exc)
                await self._send_result(tool_request_id, result, error)
            yield ev

    async def aclose(self) -> None:
        await self._stream.aclose()
```

And update `run_task_with_streaming` — drop `redis` parameter, use `client.send_tool_result`:

```python
async def run_task_with_streaming(
    client,
    renderer,
    task_id: str,
    result_timeout: float = 300.0,
    *,
    workspace: str | None = None,
) -> dict:
    """Race live stream vs fallback spinner; always return a result dict.

    When workspace is provided and client has send_tool_result, tool.request
    events are intercepted and executed against local disk before being yielded
    to the renderer.
    """
    stream = client.subscribe_events(task_id)
    try:
        first = await stream.first(timeout=FIRST_EVENT_TIMEOUT)
        if first is not None:
            if workspace is not None and hasattr(client, "send_tool_result"):
                active_stream = _ToolInterceptingStream(
                    stream, client.send_tool_result, workspace
                )
            else:
                active_stream = stream
            await renderer.stream_live(active_stream)
            return await client.get_result(task_id, timeout=result_timeout)
        with renderer.thinking("Working…"):
            return await client.get_result(task_id, timeout=result_timeout)
    finally:
        await stream.aclose()
```

- [ ] **Step 5: Update `services/cli/local_tool_executor.py`**

Delete `handle_tool_request` and its `aioredis` import. The file becomes:

```python
# services/cli/local_tool_executor.py
"""
Local filesystem tool executor for the CLI.

When the remote orchestrator emits a tool.request event the CLI runs the tool
against the user's disk via execute_local_tool and sends the result back over
the WebSocket via the send_result callback in _ToolInterceptingStream.
Only read_file / write_file / list_dir are supported; every path is confined
to the workspace root.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

LOCAL_TOOL_NAMES = frozenset({"read_file", "write_file", "list_dir"})


def _safe_path(path: str, workspace: str) -> Path:
    """Resolve `path` under `workspace`; raise ValueError if it escapes."""
    root = Path(workspace).resolve()
    candidate = (root / path).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"path {path!r} resolves outside workspace")
    return candidate


def execute_local_tool(name: str, args: dict[str, Any], *, workspace: str) -> Any:
    """Run one local file tool synchronously. Raises on bad path / unknown tool."""
    if name == "read_file":
        p = _safe_path(str(args.get("path", "")), workspace)
        return {"content": p.read_text(encoding="utf-8")}
    if name == "write_file":
        p = _safe_path(str(args.get("path", "")), workspace)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(args.get("content", "")), encoding="utf-8")
        return {"ok": True, "bytes": len(str(args.get("content", "")))}
    if name == "list_dir":
        p = _safe_path(str(args.get("path", ".")), workspace)
        return {"entries": sorted(os.listdir(p))}
    raise ValueError(f"unknown local tool: {name}")
```

- [ ] **Step 6: Remove deleted tests from `test_local_tool_executor.py`**

Delete the following from the test file (and the `redis` fixture and `fakeredis` import):
- `from unittest.mock import patch` (if only used for handle_tool_request — check)
- `import fakeredis.aioredis`
- `from services.cli.local_tool_executor import TOOL_RESULTS_PREFIX, ...` — remove `TOOL_RESULTS_PREFIX` and `handle_tool_request` from the import
- `@pytest.fixture async def redis(): ...`
- `test_handle_tool_request_writes_result`
- `test_handle_tool_request_reports_error`
- `test_tool_intercepting_stream_handles_tool_request` (old Redis version)

Add the new callback-based test (from Step 2 above).

The final import block should be:

```python
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from services.cli.local_tool_executor import execute_local_tool
```

- [ ] **Step 7: Run all CLI tests**

```bash
cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/cli/ -v 2>&1 | tail -20
```

Expected: all tests pass. The `test_event_stream.py` tests (`tail_events`, `EventStream`) are unchanged and pass. The `test_local_tool_executor.py` tests all pass with the new callback test.

- [ ] **Step 8: Commit**

```bash
git add services/cli/event_stream.py services/cli/local_tool_executor.py \
        tests/services/cli/test_event_stream.py tests/services/cli/test_local_tool_executor.py
git commit -m "refactor(cli): _ToolInterceptingStream uses send_result callback; remove Redis-dependent handle_tool_request"
```

---

### Task 4: Wire `repl.py` and `main.py` to `LabmateWSClient`; update `requirements.txt`

**Files:**
- Modify: `services/cli/repl.py`
- Modify: `services/cli/main.py`
- Modify: `tests/services/cli/test_repl_streaming.py`

After this task the active CLI code path has zero Redis calls. `redis>=5.0,<6` stays in `requirements.txt` because `event_stream.py` (the reference Redis reader) and `redis_client.py` still import it.

**`REPLContext` field rename:** `redis_url: str` → `ws_url: str`. The `REPL` creates a `LabmateWSClient` and calls `connect()` before the loop.

**Workspace:** The first path in `workspace_paths` is passed as `workspace` to `run_task_with_streaming`. If `workspace_paths` is empty, `workspace` is `None` (tools disabled for that session).

**Login flow in `main.py`:**
1. Read `LABMATE_GATEWAY_URL` env var (default `ws://localhost:8787/ws`)
2. Call `token_store.load_token()` — if valid, use it
3. Otherwise: read `LABMATE_EMAIL` + `LABMATE_PASSWORD` env vars OR prompt interactively
4. Call `LabmateWSClient.login(ws_url, email, password)` → JWT
5. Call `token_store.save_token(token)`
6. Create `LabmateWSClient(ws_url, token)` and call `await client.connect()`
7. If `auth.error` (PermissionError): clear token, re-prompt once

- [ ] **Step 1: Update `test_repl_streaming.py`** — change `redis_url` to `ws_url` in `_ctx()`

Read the current test. Change:
```python
redis_url="redis://localhost:6379/0",
```
to:
```python
ws_url="ws://localhost:8787/ws",
```

The mock `redis` in `_repl_with_mocks` continues to work because `REPL._send_task` will call `client.push_task`, `subscribe_events`, `get_result` — and those are all mocked. The field name change is the only edit needed in this test.

- [ ] **Step 2: Run test — confirm it FAILS** (REPLContext still has `redis_url`)

```bash
cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/cli/test_repl_streaming.py -v 2>&1 | tail -10
```

Expected: `TypeError: REPLContext.__init__() got an unexpected keyword argument 'ws_url'`

- [ ] **Step 3: Update `services/cli/repl.py`**

Change `REPLContext.redis_url` → `ws_url`, update `REPL.__init__` to create `LabmateWSClient`, update `REPL.run()` to connect/close, update `_send_task` to pass workspace:

```python
# services/cli/repl.py  (full replacement)
from __future__ import annotations
import asyncio
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

_AT_REF = re.compile(r"@([\w./\\\-]+)")

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style

from .identity import Identity
from .renderer import Renderer, extract_answer
from .session_store import SessionStore, SessionRecord
from .ws_client import LabmateWSClient

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
    workspace_instructions: str | None
    session_id: str
    ws_url: str
    token: str


class REPL:
    def __init__(self, ctx: REPLContext) -> None:
        self._ctx = ctx
        self._renderer = Renderer()
        self._client = LabmateWSClient(ctx.ws_url, ctx.token)
        self._sessions = SessionStore()
        self._prompt_session = PromptSession(
            history=FileHistory(str(HISTORY_PATH)),
            style=PROMPT_STYLE,
        )

    async def run(self) -> None:
        await self._client.connect()
        self._renderer.print_workspace(self._ctx.workspace_name, self._ctx.workspace_id)
        self._renderer.print_header(
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

        await self._client.aclose()

    def _run_shell(self, cmd: str) -> None:
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

    def _expand_at_refs(self, task: str) -> str:
        """Replace @path references with file/dir contents."""
        def _sub(m: re.Match) -> str:
            raw = m.group(1)
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = Path.cwd() / path
            if path.is_dir():
                files = sorted(
                    str(p.relative_to(path))
                    for p in path.rglob("*")
                    if p.is_file()
                )
                listing = "\n".join(files[:200])
                return f"<dir: {raw}>\n{listing}\n</dir>"
            if path.is_file():
                try:
                    return f"<file: {raw}>\n{path.read_text(errors='replace')}\n</file>"
                except OSError:
                    pass
            self._renderer.print_info(f"@{raw}: not found, skipped")
            return m.group(0)

        return _AT_REF.sub(_sub, task)

    async def _send_task(self, task: str) -> None:
        task = self._expand_at_refs(task)
        task_id = str(uuid.uuid4())
        turn_session_id = str(uuid.uuid4())
        self._sessions.append(SessionRecord(
            session_id=turn_session_id,
            workspace_id=self._ctx.workspace_id,
            workspace_name=self._ctx.workspace_name,
            task_preview=task[:120],
        ))

        workspace = self._ctx.workspace_paths[0] if self._ctx.workspace_paths else None

        try:
            await self._client.push_task(
                task_id=task_id,
                task=task,
                session_id=turn_session_id,
                user_id=self._ctx.identity.user_id,
                workspace_id=self._ctx.workspace_id,
            )
            from .event_stream import run_task_with_streaming
            result = await run_task_with_streaming(
                self._client, self._renderer, task_id, workspace=workspace
            )
        except Exception as exc:
            self._renderer.print_error(f"Connection error: {exc}")
            return

        if not result.get("ok"):
            self._renderer.print_error(result.get("error", "Unknown error"))
            return

        state = result.get("state", {})
        if isinstance(state, dict) and state.get("awaiting_clarification"):
            self._renderer.print_clarification(
                state.get("clarification_question") or extract_answer(state),
                session_id=turn_session_id,
            )
        else:
            answer = extract_answer(result.get("state", {}))
            self._renderer.print_answer(answer, session_id=turn_session_id)
```

- [ ] **Step 4: Update `test_repl_streaming.py`** to also add `token` field and rename the mock's internal reference

The `_repl_with_mocks` function creates `REPL.__new__(REPL)` and directly sets `r._client` (or `r._redis`). Update it to set `r._client` instead of `r._redis`:

```python
# tests/services/cli/test_repl_streaming.py (full replacement)
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from services.cli.repl import REPL, REPLContext
from services.cli.event_stream import FIRST_EVENT_TIMEOUT
from services.cli.identity import Identity


def _ctx():
    return REPLContext(
        identity=Identity(user_id="u-1", display_name="Tester"),
        workspace_id="ws-1", workspace_name="WS", workspace_paths=["/tmp/ws"],
        workspace_instructions=None, session_id="s-1",
        ws_url="ws://localhost:8787/ws",
        token="tok",
    )


def _repl_with_mocks(first_event, result):
    r = REPL.__new__(REPL)
    r._ctx = _ctx()
    r._renderer = MagicMock()
    r._renderer.stream_live = AsyncMock()
    r._renderer.print_answer = MagicMock()
    r._renderer.print_error = MagicMock()
    r._renderer.thinking = MagicMock()
    r._sessions = MagicMock()
    r._sessions.append = MagicMock()

    client = MagicMock()
    client.push_task = AsyncMock()
    client.get_result = AsyncMock(return_value=result)
    client.send_tool_result = AsyncMock()

    stream = MagicMock()
    stream.first = AsyncMock(return_value=first_event)
    stream.aclose = AsyncMock()
    client.subscribe_events = MagicMock(return_value=stream)
    r._client = client
    return r, client, stream


@pytest.mark.asyncio
async def test_send_task_streams_when_first_event_arrives():
    first = {"type": "turn.start", "task": "what is the answer?"}
    result = {"ok": True, "state": {"final_answer": "42"}}
    r, client, stream = _repl_with_mocks(first, result)

    await r._send_task("what is the answer?")

    client.push_task.assert_awaited_once()
    r._renderer.stream_live.assert_awaited_once()
    stream.aclose.assert_awaited_once()
    r._renderer.print_answer.assert_called_once()
    assert r._renderer.print_answer.call_args.args[0] == "42"


@pytest.mark.asyncio
async def test_send_task_falls_back_when_no_event():
    result = {"ok": True, "state": {"final_answer": "fallback-answer"}}
    r, client, stream = _repl_with_mocks(None, result)

    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=None)
    cm.__exit__ = MagicMock(return_value=False)
    r._renderer.thinking = MagicMock(return_value=cm)

    await r._send_task("hi")

    r._renderer.stream_live.assert_not_called()
    stream.aclose.assert_awaited_once()
    client.get_result.assert_awaited()
    r._renderer.print_answer.assert_called_once()
    assert r._renderer.print_answer.call_args.args[0] == "fallback-answer"


@pytest.mark.asyncio
async def test_send_task_reports_error_result():
    result = {"ok": False, "error": "task_failed"}
    r, client, stream = _repl_with_mocks(None, result)
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=None)
    cm.__exit__ = MagicMock(return_value=False)
    r._renderer.thinking = MagicMock(return_value=cm)

    await r._send_task("boom")

    r._renderer.print_error.assert_called_once_with("task_failed")


def test_first_event_timeout_constant_is_reasonable():
    assert 0 < FIRST_EVENT_TIMEOUT <= 5.0
```

- [ ] **Step 5: Run repl tests — all pass**

```bash
cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/cli/test_repl_streaming.py -v 2>&1 | tail -10
```

Expected: `4 passed`

- [ ] **Step 6: Update `services/cli/main.py`**

Replace the `_redis_url()` helper and all `LabmateRedisClient` usages with `LabmateWSClient` + login flow. Replace `redis_url` with `ws_url` + `token` in `REPLContext` construction. Add a `_get_token()` helper that loads from cache or prompts.

```python
# services/cli/main.py  (full replacement)
"""
Labmate CLI — interactive agent session.

Usage:
    python -m services.cli                   # start REPL with workspace picker
    python -m services.cli --resume s-abc    # resume a previous session
    python -m services.cli "do this task"    # one-shot (no REPL)

Environment variables:
    LABMATE_GATEWAY_URL   ws_gateway WebSocket URL (default ws://localhost:8787/ws)
    LABMATE_EMAIL         Login email (prompted if absent and no cached token)
    LABMATE_PASSWORD      Login password (prompted if absent and no cached token)
"""
from __future__ import annotations
import asyncio
import getpass
import json
import os
import uuid
from pathlib import Path
from typing import Optional

import typer

from .identity import load_or_create_identity, Identity
from .renderer import Renderer, extract_answer
from .repl import REPL, REPLContext
from .token_store import clear_token, load_token, save_token
from .workspace_picker import pick_workspace
from .ws_client import LabmateWSClient

app = typer.Typer(add_completion=False, help="Labmate — autonomous agent CLI")
_renderer = Renderer()

_WS_CACHE = Path.home() / ".labmate" / "workspaces.json"


def _gateway_url() -> str:
    return os.getenv("LABMATE_GATEWAY_URL", "ws://localhost:8787/ws")


def _load_workspaces(user_id: str) -> list[dict]:
    if not _WS_CACHE.exists():
        return []
    try:
        all_ws = json.loads(_WS_CACHE.read_text())
        return [w for w in all_ws if w.get("user_id") == user_id]
    except Exception:
        return []


def _default_workspace(user_id: str) -> dict:
    ws = {
        "workspace_id": "default",
        "name": "default",
        "paths": [os.getcwd()],
        "instructions": "",
        "user_id": user_id,
    }
    _save_workspace(ws)
    return ws


def _save_workspace(ws: dict) -> None:
    existing = []
    if _WS_CACHE.exists():
        try:
            existing = json.loads(_WS_CACHE.read_text())
        except Exception:
            pass
    if not any(
        w.get("workspace_id") == ws["workspace_id"]
        and w.get("user_id") == ws.get("user_id")
        for w in existing
    ):
        existing.append(ws)
    _WS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _WS_CACHE.write_text(json.dumps(existing, indent=2))


async def _get_token(ws_url: str) -> str:
    """Return a valid JWT: from cache, from env vars, or by interactive prompt."""
    token = load_token()
    if token:
        return token

    email = os.getenv("LABMATE_EMAIL", "")
    password = os.getenv("LABMATE_PASSWORD", "")

    if not email:
        email = input("Email: ").strip()
    if not password:
        password = getpass.getpass("Password: ")

    token = await LabmateWSClient.login(ws_url, email, password)
    save_token(token)
    return token


@app.command()
def main(
    prompt: Optional[str] = typer.Argument(None, help="One-shot task (skips REPL)"),
    resume: Optional[str] = typer.Option(None, "--resume", "-r", help="Resume session ID"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Workspace ID"),
) -> None:
    asyncio.run(_async_main(prompt, resume, workspace))


async def _async_main(
    one_shot: str | None,
    resume_id: str | None,
    workspace_id_flag: str | None,
) -> None:
    ws_url = _gateway_url()
    identity = load_or_create_identity()
    existing_ws = _load_workspaces(identity.user_id)

    try:
        token = await _get_token(ws_url)
    except Exception as exc:
        _renderer.print_error(f"Login failed: {exc}")
        raise SystemExit(1)

    # ── workspace resolution (unchanged logic) ─────────────────────────────
    if resume_id:
        from .session_store import SessionStore
        prior_sessions = SessionStore().list()
        prior = next((s for s in prior_sessions if s.session_id == resume_id), None)
        if prior is None:
            _renderer.print_info(f"Session {resume_id[:8]}… not found — pick a workspace.")
        if prior:
            prior_ws = next((w for w in existing_ws
                            if w.get("workspace_id") == prior.workspace_id), None)
            if prior_ws is None:
                _renderer.print_info(f"Session {resume_id[:8]}… found but workspace not in local cache.")
            if prior_ws:
                _renderer.print_info(f"Resuming session {resume_id[:8]}… (workspace: {prior_ws['name']})")
                ctx = REPLContext(
                    identity=Identity(user_id=identity.user_id, display_name=identity.display_name),
                    workspace_id=prior_ws["workspace_id"],
                    workspace_name=prior_ws["name"],
                    workspace_paths=prior_ws.get("paths", []),
                    workspace_instructions=prior_ws.get("instructions"),
                    session_id=resume_id,
                    ws_url=ws_url,
                    token=token,
                )
                await REPL(ctx).run()
                return

    if workspace_id_flag:
        match = next((w for w in existing_ws if w["workspace_id"] == workspace_id_flag), None)
        if match:
            ws_choice_raw = match
        else:
            ws_choice_raw = {
                "workspace_id": workspace_id_flag,
                "name": workspace_id_flag,
                "paths": [os.getcwd()],
                "instructions": "",
                "user_id": identity.user_id,
            }
            _save_workspace(ws_choice_raw)
            _renderer.print_info(f"Workspace '{workspace_id_flag}' not found — created a seeded workspace.")
    elif one_shot or not existing_ws:
        ws_choice_raw = _default_workspace(identity.user_id)
    else:
        from .workspace_picker import WorkspaceChoice
        ws_choice = pick_workspace(existing_ws)
        ws_choice_raw = {
            "workspace_id": ws_choice.workspace_id,
            "name": ws_choice.name,
            "paths": ws_choice.paths,
            "instructions": ws_choice.instructions,
            "user_id": identity.user_id,
        }
        _save_workspace(ws_choice_raw)

    session_id = resume_id or str(uuid.uuid4())
    if not resume_id:
        _renderer.print_header(f"Session: {session_id}  (resume with --resume {session_id})")

    if one_shot:
        from .event_stream import run_task_with_streaming
        client = LabmateWSClient(ws_url, token)
        try:
            await client.connect()
        except PermissionError as exc:
            clear_token()
            _renderer.print_error(f"Auth failed: {exc}")
            raise SystemExit(1)

        task_id = str(uuid.uuid4())
        _renderer.print_workspace(ws_choice_raw["name"], ws_choice_raw["workspace_id"])
        workspace = ws_choice_raw.get("paths", [None])[0]
        try:
            await client.push_task(
                task_id=task_id,
                task=one_shot,
                session_id=session_id,
                user_id=identity.user_id,
                workspace_id=ws_choice_raw["workspace_id"],
            )
            result = await run_task_with_streaming(
                client, _renderer, task_id, workspace=workspace
            )
        except Exception as exc:
            _renderer.print_error(f"Connection error: {exc}")
            await client.aclose()
            raise SystemExit(1)
        await client.aclose()
        if not result.get("ok"):
            _renderer.print_error(result.get("error", "unknown"))
            raise SystemExit(1)
        state = result.get("state", {})
        if isinstance(state, dict) and state.get("awaiting_clarification"):
            _renderer.print_clarification(
                state.get("clarification_question") or extract_answer(state),
                session_id=session_id,
            )
        else:
            _renderer.print_answer(extract_answer(state), session_id=session_id)
        return

    ctx = REPLContext(
        identity=Identity(user_id=identity.user_id, display_name=identity.display_name),
        workspace_id=ws_choice_raw["workspace_id"],
        workspace_name=ws_choice_raw["name"],
        workspace_paths=ws_choice_raw.get("paths", []),
        workspace_instructions=ws_choice_raw.get("instructions"),
        session_id=session_id,
        ws_url=ws_url,
        token=token,
    )
    await REPL(ctx).run()


if __name__ == "__main__":
    app()
```

- [ ] **Step 7: Run the full CLI test suite**

```bash
cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/cli/ -v 2>&1 | tail -25
```

Expected: all tests pass. Key groups:
- `test_token_store.py` — 6 passed
- `test_ws_client.py` — all passed
- `test_event_stream.py` — 5 passed (EventStream + tail_events tests unchanged)
- `test_local_tool_executor.py` — 7 passed (execute_local_tool tests + 1 new callback test)
- `test_repl_streaming.py` — 4 passed

If any fail, diagnose and fix before committing.

- [ ] **Step 8: Commit**

```bash
git add services/cli/repl.py services/cli/main.py tests/services/cli/test_repl_streaming.py
git commit -m "feat(cli): switch REPL + one-shot to LabmateWSClient; login via ws_gateway auth"
```

---

## Verification (whole plan)

- [ ] **Full CLI test suite passes:**

```bash
cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/cli/ -q 2>&1 | tail -5
```

Expected: all pass, 0 failed.

- [ ] **No Redis calls in the active code path** (confirm no `redis://` URLs or `aioredis` imports are referenced from `repl.py` → `ws_client.py` → `event_stream._ToolInterceptingStream` → `local_tool_executor.execute_local_tool`):

```bash
grep -n "aioredis\|redis://" \
  services/cli/repl.py \
  services/cli/main.py \
  services/cli/ws_client.py \
  services/cli/event_stream.py \
  services/cli/local_tool_executor.py
```

Expected: no output (zero matches in those five files).

- [ ] **ws_gateway / ws_tests unaffected:**

```bash
cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/ws_gateway/ -q 2>&1 | tail -5
```

Expected: all pass.

- [ ] **Manual data-flow trace (no code):** CLI starts → `_get_token` returns JWT → `LabmateWSClient.connect()` sends `{type:'auth',token}` → receives `auth.ok` → `push_task` sends `{type:'send',text,sessionId}` → `WSEventStream.events()` receives WS frames, normalises to snake_case → `StreamRenderer` renders live → if `tool.request` arrives, `_ToolInterceptingStream` calls `execute_local_tool` then `client.send_tool_result` → ws_gateway writes to `labmate:tool-results:<task_id>` → orchestrator resumes → `turn.done` → `get_result` synthesises `{"ok":true,"state":{"final_answer":…}}` → `print_answer` displays. Redis never accessed by CLI.
