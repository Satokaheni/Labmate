from __future__ import annotations

import asyncio
import os
import uuid
from typing import Awaitable, Callable

import redis.asyncio as aioredis
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from services.ws_gateway.auth import AuthService, build_auth_router
from services.ws_gateway.boot import CheckFn, run_boot_sequence
from services.ws_gateway.config import Config
from services.ws_gateway.redis_bridge import push_task, tail_task_events, translate_event
from services.ws_gateway.sessions import InMemorySessionStore, build_sessions_router


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _relay_task(
    ws: WebSocket,
    redis: aioredis.Redis,
    task_id: str,
    turn_id: str,
) -> None:
    """Tail the orchestrator event stream for one task and relay StreamEvents."""
    async for raw in tail_task_events(redis, task_id, block_ms=200):
        framed = translate_event(raw, turn_id=turn_id)
        if framed is not None:
            await ws.send_json(framed)


async def _handle_send(
    ws: WebSocket,
    redis: aioredis.Redis,
    msg: dict,
) -> None:
    task_id = "task-" + uuid.uuid4().hex[:12]
    turn_id = "turn-" + uuid.uuid4().hex[:12]
    session_id = msg.get("sessionId", "")
    text = msg.get("text", "")

    # Tell the client a turn was created so it can render the user bubble + thinking.
    await ws.send_json(
        {
            "type": "turn.created",
            "turn": {
                "id": turn_id,
                "sessionId": session_id,
                "role": "user",
                "text": text,
                "createdAt": _now_iso(),
                "status": "streaming",
            },
        }
    )

    await push_task(redis, task_id, task=text, session_id=session_id)
    await _relay_task(ws, redis, task_id, turn_id)


async def _ws_loop(
    ws: WebSocket,
    auth: AuthService,
    redis: aioredis.Redis,
    boot_checks: dict[str, CheckFn],
) -> None:
    # ── auth handshake: first frame MUST be {type:'auth',token} ────────────
    first = await ws.receive_json()
    if first.get("type") != "auth" or auth.verify_token(first.get("token", "")) is None:
        await ws.send_json({"type": "auth.error", "reason": "invalid"})
        await ws.close()
        return

    await ws.send_json({"type": "auth.ok", "user": auth.user_record()})

    # ── boot sequence ──────────────────────────────────────────────────────
    async def emit(ev: dict) -> None:
        await ws.send_json(ev)

    await run_boot_sequence(emit, boot_checks)

    # ── client message loop ────────────────────────────────────────────────
    while True:
        msg = await ws.receive_json()
        mtype = msg.get("type")
        if mtype == "send":
            await _handle_send(ws, redis, msg)
        elif mtype == "cancel":
            await ws.send_json({"type": "turn.done", "turnId": msg.get("turnId", ""), "status": "error"})
        elif mtype in ("session.new", "session.open", "session.rename", "debug.set"):
            continue
        else:
            continue


def build_app(
    config: Config,
    *,
    redis: aioredis.Redis | None = None,
    boot_checks: dict[str, CheckFn] | None = None,
    session_store: InMemorySessionStore | None = None,
) -> FastAPI:
    app = FastAPI(title="labmate-ws-gateway")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    auth = AuthService(config)
    store = session_store or InMemorySessionStore()
    r = redis or aioredis.from_url(config.redis_url, decode_responses=True)

    # default boot checks bind the live redis; brain check needs an http_get
    if boot_checks is None:
        from services.ws_gateway import boot as boot_mod

        async def _http_get(url: str):  # pragma: no cover - real network
            import urllib.request

            def _do() -> object:
                return urllib.request.urlopen(url, timeout=2)

            return await asyncio.to_thread(_do)

        boot_checks = {
            "brain": lambda: boot_mod.check_brain(http_get=_http_get),
            "nervous_system": lambda: boot_mod.check_nervous_system(mcp_ready=True),
            "hands": lambda: boot_mod.check_hands(),
            "memory": lambda: boot_mod.check_memory(redis=r),
            "workspace": lambda: boot_mod.check_workspace(),
        }

    app.state.auth = auth
    app.state.redis = r
    app.state.store = store

    app.include_router(build_auth_router(auth))
    app.include_router(build_sessions_router(store))

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        try:
            await _ws_loop(ws, auth, r, boot_checks)
        except WebSocketDisconnect:
            return

    return app


def create_app() -> FastAPI:
    """uvicorn entrypoint: `uvicorn services.ws_gateway.server:create_app --factory`."""
    return build_app(Config.from_env())


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "services.ws_gateway.server:create_app",
        factory=True,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8787")),
    )
