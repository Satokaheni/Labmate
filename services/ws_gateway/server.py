from __future__ import annotations

import asyncio
import json
import os
import time as _time
import uuid
from datetime import UTC

import redis.asyncio as aioredis
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from services.ws_gateway.auth import AuthService, build_auth_router
from services.ws_gateway.boot import CheckFn, run_boot_sequence
from services.ws_gateway.config import Config
from services.ws_gateway.redis_bridge import (
    push_task,
    tail_task_events,
    translate_event,
    write_cancel,
    write_steer,
    write_tool_result,
)
from services.ws_gateway.sessions import InMemorySessionStore, build_sessions_router
from services.ws_gateway.user_store import (
    InMemoryUserStore,
    MongoUserStore,
    SqliteUserStore,
    UserStore,
)


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _relay_task(
    ws: WebSocket,
    redis: aioredis.Redis,
    task_id: str,
    turn_id: str,
    *,
    debug: bool = False,
) -> None:
    """Tail the orchestrator event stream for one task and relay StreamEvents.

    Synthesizes reasoning.done from accumulated reasoning events before turn.done.
    """
    reasoning_chunks: list[str] = []
    reasoning_node: str = "chat_node"
    reasoning_start: float | None = None

    async for raw in tail_task_events(redis, task_id, block_ms=200):
        etype = raw.get("type")

        # Accumulate reasoning text for synthesis
        if etype == "reasoning":
            reasoning_chunks.append(raw.get("text", ""))
            reasoning_node = raw.get("node", reasoning_node)
            if reasoning_start is None:
                reasoning_start = _time.time()

        # Synthesize reasoning.done before relaying turn.done
        if etype == "turn.done" and reasoning_chunks:
            full_text = "".join(reasoning_chunks)
            duration_ms = int((_time.time() - (reasoning_start or _time.time())) * 1000)
            first_line = next(
                (ln.strip() for ln in full_text.splitlines() if ln.strip()),
                full_text[:120],
            )
            await ws.send_json(
                {
                    "type": "reasoning.done",
                    "turnId": turn_id,
                    "reasoning": {
                        "summary": first_line[:120],
                        "text": full_text,
                        "node": reasoning_node,
                        "tokens": len(full_text) // 4,
                        "budget": 0,
                        "durationMs": duration_ms,
                    },
                }
            )
            reasoning_chunks = []

        # Emit tool.frame when debug mode is active
        if debug and etype in ("tool.start", "tool.done"):
            tool_id = raw.get("tool_id", "")
            if etype == "tool.start":
                frame_payload = {"name": raw.get("name", ""), "args": raw.get("args", {})}
                frame_dir = "out"
            else:
                frame_payload = {"result": raw.get("result"), "status": raw.get("status", "done")}
                frame_dir = "in"
            await ws.send_json(
                {
                    "type": "tool.frame",
                    "turnId": turn_id,
                    "toolId": tool_id,
                    "frame": {
                        "dir": frame_dir,
                        "method": "tools/call",
                        "payload": frame_payload,
                        "ts": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
                    },
                }
            )

        framed = translate_event(raw, turn_id=turn_id)
        if framed is not None:
            await ws.send_json(framed)


async def _handle_send(
    ws: WebSocket,
    redis: aioredis.Redis,
    msg: dict,
    *,
    store: InMemorySessionStore,
    active_session_id: str | None = None,
    debug: bool = False,
) -> tuple[str, asyncio.Task]:
    task_id = "task-" + uuid.uuid4().hex[:12]
    user_turn_id = "turn-" + uuid.uuid4().hex[:12]
    assistant_turn_id = "turn-" + uuid.uuid4().hex[:12]
    session_id = msg.get("sessionId", "") or active_session_id or ""
    text = msg.get("text", "")

    user_turn = {
        "id": user_turn_id,
        "sessionId": session_id,
        "role": "user",
        "text": text,
        "createdAt": _now_iso(),
        "status": "complete",
    }

    if session_id:
        store.add_turn(session_id, user_turn)
        session = store.get(session_id)
        if session:
            await ws.send_json({"type": "session.updated", "session": session})

    await ws.send_json({"type": "turn.created", "turn": user_turn})

    assistant_turn = {
        "id": assistant_turn_id,
        "sessionId": session_id,
        "role": "assistant",
        "text": "",
        "createdAt": _now_iso(),
        "status": "streaming",
    }
    await ws.send_json({"type": "turn.created", "turn": assistant_turn})

    await push_task(redis, task_id, task=text, session_id=session_id)
    relay = asyncio.create_task(_relay_task(ws, redis, task_id, assistant_turn_id, debug=debug))
    return task_id, relay


async def _ws_loop(
    ws: WebSocket,
    auth: AuthService,
    redis: aioredis.Redis,
    boot_checks: dict[str, CheckFn],
    store: InMemorySessionStore,
) -> None:
    # ── auth handshake: first frame MUST be {type:'auth',token} ────────────
    first = await ws.receive_json()
    claims = auth.verify_token(first.get("token", ""))
    if first.get("type") != "auth" or claims is None:
        await ws.send_json({"type": "auth.error", "reason": "invalid"})
        await ws.close()
        return

    await ws.send_json(
        {
            "type": "auth.ok",
            "user": {
                "id": claims["sub"],
                "email": claims["email"],
                "role": claims.get("role", "user"),
            },
        }
    )

    # ── boot sequence ──────────────────────────────────────────────────────
    async def emit(ev: dict) -> None:
        await ws.send_json(ev)

    await run_boot_sequence(emit, boot_checks, session_store=store)

    # ── client message loop ────────────────────────────────────────────────
    active_task_id: str | None = None
    active_session_id: str | None = None
    relay: asyncio.Task | None = None
    # debug_mode tracks the last explicitly-set debug state for the active session
    debug_mode: bool = False
    while True:
        msg = await ws.receive_json()
        mtype = msg.get("type")
        if mtype == "send":
            # Await the previous relay if one is still running (one turn at a time).
            if relay is not None and not relay.done():
                await relay
            debug_on = store.get_debug(active_session_id or "") if active_session_id else debug_mode
            active_task_id, relay = await _handle_send(
                ws, redis, msg, store=store, active_session_id=active_session_id, debug=debug_on
            )
        elif mtype == "tool.result":
            if active_task_id is not None:
                await write_tool_result(
                    redis,
                    active_task_id,
                    msg.get("toolRequestId", ""),
                    msg.get("result"),
                    msg.get("error"),
                )
        elif mtype == "cancel":
            turn_id_to_cancel = msg.get("turnId", "")
            # Stop the relay task so no more events flow to the client
            if relay is not None and not relay.done():
                relay.cancel()
            # Write Redis cancel flag so the orchestrator can detect cancellation
            if active_task_id is not None:
                await write_cancel(redis, active_task_id)
            await ws.send_json(
                {"type": "turn.done", "turnId": turn_id_to_cancel, "status": "error"}
            )
            relay = None
            active_task_id = None
        elif mtype == "steer":
            # Out-of-band steer: deliver a mid-turn user instruction to the
            # running task. The orchestrator drains it at the top of its next
            # ReAct turn and injects it as a marked user message — the relay
            # keeps streaming, the turn is NOT cancelled.
            steer_text = msg.get("text", "")
            if active_task_id is not None and steer_text:
                await write_steer(redis, active_task_id, steer_text)
            await ws.send_json({"type": "steer.ack", "taskId": active_task_id or ""})
        elif mtype == "session.new":
            mode = msg.get("mode", "chat")
            session = store.create(title="New session", mode=mode)
            active_session_id = session["id"]
            await ws.send_json({"type": "session.updated", "session": session})
        elif mtype == "session.open":
            sid = msg.get("sessionId", "")
            session = store.get(sid)
            if session is not None:
                active_session_id = sid
                await ws.send_json({"type": "session.updated", "session": session})
        elif mtype == "session.rename":
            sid = msg.get("sessionId", "")
            title = msg.get("title", "")
            session = store.rename(sid, title)
            if session is not None:
                await ws.send_json({"type": "session.updated", "session": session})
        elif mtype == "debug.set":
            sid = msg.get("sessionId", "") or active_session_id or ""
            enabled = bool(msg.get("enabled", False))
            debug_mode = enabled
            if sid:
                store.set_debug(sid, enabled)
        elif mtype == "compact":
            if not active_session_id:
                await ws.send_json({"type": "compact.done", "ok": False, "error": "no_session"})
                continue
            task_id = "compact-" + uuid.uuid4().hex[:12]
            result_key = f"labmate:result:{task_id}"

            # Subscribe BEFORE pushing to goals so we never miss the PUBLISH
            pubsub = redis.pubsub()
            await pubsub.subscribe(result_key)
            try:
                await redis.xadd(
                    "labmate:goals",
                    {
                        "payload": json.dumps(
                            {
                                "task_id": task_id,
                                "kind": "compact",
                                "session_id": active_session_id,
                                "user_id": claims["sub"],
                                "workspace_id": "",
                            }
                        ),
                    },
                )

                async def _await_result() -> dict | None:
                    async for pmsg in pubsub.listen():
                        if pmsg["type"] == "message":
                            raw = await redis.get(result_key)
                            return json.loads(raw) if raw else None
                    return None

                try:
                    result_dict = await asyncio.wait_for(_await_result(), timeout=60.0)
                except TimeoutError:
                    result_dict = None
            finally:
                await pubsub.aclose()

            if result_dict:
                await ws.send_json({"type": "compact.done", **result_dict})
            else:
                await ws.send_json({"type": "compact.done", "ok": False, "error": "timeout"})
        else:
            continue


def _build_user_store(config: Config) -> UserStore:
    kind = config.user_store
    if kind == "memory":
        return InMemoryUserStore()
    if kind == "mongo":
        return MongoUserStore(config.mongo_url)
    if kind == "sqlite":
        return SqliteUserStore(os.path.join(config.data_dir, "users.db"))
    raise ValueError(f"unknown USER_STORE: {kind!r}")


def build_app(
    config: Config,
    *,
    redis: aioredis.Redis | None = None,
    boot_checks: dict[str, CheckFn] | None = None,
    session_store: InMemorySessionStore | None = None,
    user_store=None,
) -> FastAPI:
    app = FastAPI(title="labmate-ws-gateway")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if user_store is None:
        user_store = _build_user_store(config)
    auth = AuthService(config, user_store)
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

    @app.on_event("startup")
    async def _seed_admin() -> None:
        if await user_store.count() == 0 and config.admin_password:
            await auth.create_user(
                config.admin_email,
                config.admin_password,
                display_name="Admin",
                role="admin",
            )

    app.include_router(build_auth_router(auth))
    app.include_router(build_sessions_router(store))

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        try:
            await _ws_loop(ws, auth, r, boot_checks, store)
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
