# services/cli/ws_client.py
from __future__ import annotations

import asyncio
import contextlib
import json
import urllib.request
from collections.abc import AsyncIterator
from typing import Any

import websockets
import websockets.exceptions

# ── Event normalisation ────────────────────────────────────────────────────────


def _normalize_ws_event(ev: dict) -> dict:
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

    return ev


# ── WSEventStream ──────────────────────────────────────────────────────────────


class WSEventStream:
    def __init__(self, ws) -> None:
        self._ws = ws
        self._pending: dict | None = None
        self._done: bool = False
        self._events: list[dict] = []

    async def _recv(self) -> dict | None:
        try:
            raw = await self._ws.recv()
        except Exception:
            return None  # connection closed → end stream
        try:
            return _normalize_ws_event(json.loads(raw))
        except json.JSONDecodeError:
            return {}  # malformed frame — events() will skip

    async def first(self, timeout: float) -> dict | None:
        try:
            ev = await asyncio.wait_for(self._recv(), timeout=timeout)
        except TimeoutError:
            return None
        if ev is None:
            return None
        self._events.append(ev)
        self._pending = ev
        return ev

    async def events(self) -> AsyncIterator[dict]:
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
            if not ev:
                continue
            self._events.append(ev)
            yield ev
            if ev.get("type") == "turn.done":
                self._done = True
                return

    async def aclose(self) -> None:
        pass

    def result(self) -> dict:
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
    return ws_url.replace("wss://", "https://").replace("ws://", "http://").removesuffix("/ws")


async def _http_post_json(url: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})

    def _do() -> dict:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    return await asyncio.to_thread(_do)


class LabmateWSClient:
    def __init__(self, ws_url: str, token: str) -> None:
        self._ws_url = ws_url
        self._token = token
        self._ws: Any = None
        self._current_stream: WSEventStream | None = None

    @classmethod
    async def login(cls, ws_url: str, email: str, password: str) -> str:
        base = _ws_to_http(ws_url)
        resp = await _http_post_json(f"{base}/auth/login", {"email": email, "password": password})
        return resp["token"]

    async def connect(self) -> None:
        self._ws = await websockets.connect(self._ws_url)
        await self._ws.send(json.dumps({"type": "auth", "token": self._token}))
        raw = await self._ws.recv()
        frame = json.loads(raw)
        if frame.get("type") == "auth.error":
            await self._ws.close()
            raise PermissionError(f"ws_gateway auth rejected: {frame.get('reason', 'unknown')}")

    async def push_task(
        self,
        task_id: str,
        task: str,
        session_id: str,
        user_id: str = "",
        workspace_id: str = "",
    ) -> None:
        # Single-turn model: task_id/user_id/workspace_id accepted for a
        # stable call signature but not forwarded — ws_gateway routes events
        # to this connection by session.
        self._current_stream = WSEventStream(self._ws)
        await self._ws.send(
            json.dumps(
                {
                    "type": "send",
                    "text": task,
                    "sessionId": session_id,
                }
            )
        )

    def subscribe_events(self, task_id: str) -> WSEventStream:
        if self._current_stream is None:
            raise RuntimeError("subscribe_events called before push_task")
        return self._current_stream

    async def get_result(self, task_id: str, timeout: float = 300.0) -> dict:
        stream = self._current_stream
        if stream is None:
            return {"ok": False, "error": "no_active_turn"}
        if not stream._done:
            try:
                async with asyncio.timeout(timeout):
                    async for _ in stream.events():
                        pass
            except TimeoutError:
                return {"ok": False, "error": "timeout"}
        return stream.result()

    async def send_tool_result(
        self,
        tool_request_id: str,
        result: Any,
        error: str | None,
    ) -> None:
        await self._ws.send(
            json.dumps(
                {
                    "type": "tool.result",
                    "toolRequestId": tool_request_id,
                    "result": result,
                    "error": error,
                }
            )
        )

    async def aclose(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
