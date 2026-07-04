from __future__ import annotations

import asyncio
import logging
import os
import time as _time
import uuid
from datetime import UTC
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from services.orchestrator import local_tools
from services.orchestrator.inproc_bus import Subscription
from services.ws_gateway.auth import AuthService, build_auth_router
from services.ws_gateway.boot import CheckFn, run_boot_sequence
from services.ws_gateway.config import Config
from services.ws_gateway.event_translate import translate_event
from services.ws_gateway.sessions import InMemorySessionStore, build_sessions_router

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _relay_task(
    ws: WebSocket,
    sub: Subscription,
    task_id: str,
    turn_id: str,
    *,
    debug: bool = False,
    store=None,
    session_id: str = "",
    runtime: Any = None,
    workspace_root: str | None = None,
) -> None:
    """Relay one task's in-process event-bus subscription as StreamEvents.

    `sub` must already be subscribed (via `runtime.bus.subscribe(f"events:{task_id}")`)
    BEFORE the goal was submitted, so no early events (e.g. turn.start) are lost —
    the bus is post-subscribe-only and does not replay history.

    Synthesizes reasoning.done from accumulated reasoning events before turn.done.
    Assembles and persists the complete assistant turn on turn.done (best-effort).
    """
    reasoning_chunks: list[str] = []
    reasoning_node: str = "chat_node"
    reasoning_start: float | None = None
    reasoning_obj: dict | None = None  # Structured reasoning object for persistence

    # Accumulate assistant turn data for persistence
    answer_chunks: list[str] = []
    tool_calls: dict[str, dict] = {}  # Keyed by tool_id for updates

    try:
        async for raw in sub:
            etype = raw.get("type")

            # Accumulate answer text deltas
            if etype == "answer.delta":
                answer_chunks.append(raw.get("text", ""))

            # Accumulate reasoning text for synthesis
            if etype == "reasoning":
                reasoning_chunks.append(raw.get("text", ""))
                reasoning_node = raw.get("node", reasoning_node)
                if reasoning_start is None:
                    reasoning_start = _time.time()

            # Track tool calls: start creates entry, done updates it
            if etype == "tool.start":
                tool_id = raw.get("tool_id", "")
                tool_calls[tool_id] = {
                    "id": tool_id,
                    "name": raw.get("name", ""),
                    "args": raw.get("args", {}),
                    "result": None,
                    "status": "pending",
                }
            elif etype == "tool.done":
                tool_id = raw.get("tool_id", "")
                if tool_id in tool_calls:
                    tool_calls[tool_id]["result"] = raw.get("result")
                    tool_calls[tool_id]["status"] = raw.get("status", "done")

            # Synthesize reasoning.done before relaying turn.done
            if etype == "turn.done" and reasoning_chunks:
                full_text = "".join(reasoning_chunks)
                duration_ms = int((_time.time() - (reasoning_start or _time.time())) * 1000)
                first_line = next(
                    (ln.strip() for ln in full_text.splitlines() if ln.strip()),
                    full_text[:120],
                )
                reasoning_obj = {
                    "summary": first_line[:120],
                    "text": full_text,
                    "node": reasoning_node,
                    "tokens": len(full_text) // 4,
                    "budget": 0,
                    "durationMs": duration_ms,
                }
                await ws.send_json(
                    {
                        "type": "reasoning.done",
                        "turnId": turn_id,
                        "reasoning": reasoning_obj,
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
                    frame_payload = {
                        "result": raw.get("result"),
                        "status": raw.get("status", "done"),
                    }
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

            # On turn.done, assemble and persist the complete assistant turn (best-effort)
            if etype == "turn.done":
                if session_id and store is not None:
                    # Prefer final_answer from the event; fall back to concatenated deltas
                    final_answer = raw.get("final_answer")
                    text = final_answer if final_answer else "".join(answer_chunks)

                    # Build the turn document
                    assistant_turn = {
                        "id": turn_id,
                        "sessionId": session_id,
                        "role": "assistant",
                        "text": text,
                        "reasoning": reasoning_obj,
                        "toolCalls": list(tool_calls.values()),
                        "createdAt": _now_iso(),
                        "status": raw.get("status", "complete"),
                    }

                    # Persist best-effort: log on failure, do not propagate
                    try:
                        await store.add_turn(session_id, assistant_turn)
                    except Exception as e:
                        logger.warning(
                            "Failed to persist assistant turn for session %s: %s", session_id, e
                        )
                break
    finally:
        sub.close()


def _default_session_store(config: Config):
    """SQLite-backed session store over the shared LocalStore (local harness)."""
    from services.orchestrator.local_store import get_local_store
    from services.ws_gateway.sqlite_session_store import SqliteSessionStore

    return SqliteSessionStore(get_local_store())


def _title_from_message(text: str, max_len: int = 48) -> str:
    """Derive a chat title from the first user message (Claude-style auto-title)."""
    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if not first_line:
        return "New session"
    return first_line[:max_len].rstrip() + ("…" if len(first_line) > max_len else "")


async def _handle_send(
    ws: WebSocket,
    runtime: Any,
    msg: dict,
    *,
    store: InMemorySessionStore,
    active_session_id: str | None = None,
    debug: bool = False,
    client_capabilities: dict | None = None,
) -> tuple[str, asyncio.Task]:
    task_id = "task-" + uuid.uuid4().hex[:12]
    user_turn_id = "turn-" + uuid.uuid4().hex[:12]
    assistant_turn_id = "turn-" + uuid.uuid4().hex[:12]
    session_id = msg.get("sessionId", "") or active_session_id or ""
    text = msg.get("text", "")

    # Auto-create the session on first send (Claude-style new chat): if the
    # client's session id is unknown, mint it now, titled from the first message.
    # The add_turn block below then emits session.updated with the titled session.
    if session_id and await store.get(session_id) is None:
        await store.create(
            title=_title_from_message(text),
            mode=msg.get("mode", "chat"),
            session_id=session_id,
        )

    user_turn = {
        "id": user_turn_id,
        "sessionId": session_id,
        "role": "user",
        "text": text,
        "createdAt": _now_iso(),
        "status": "complete",
    }

    if session_id:
        await store.add_turn(session_id, user_turn)
        session = await store.get(session_id)
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

    workspace_root = msg.get("workspaceRoot", "")

    # Pre-subscribe BEFORE submitting the goal: the EventBus is post-subscribe-only
    # (no replay), so subscribing after submit_goal risks losing early events like
    # turn.start if the orchestrator's goal loop runs before we get to subscribe.
    sub = runtime.bus.subscribe(f"events:{task_id}")
    await runtime.submit_goal(
        {
            "task_id": task_id,
            "task": text,
            "session_id": session_id,
            "client_capabilities": client_capabilities,
            "workspace_root": workspace_root,
        }
    )

    # This relay is the rich turn-writer for this session; tell the
    # orchestrator's fallback writer (_persist_turns) to skip (hermes skip_db).
    sig = getattr(runtime, "signals", None)
    if sig is not None and session_id:
        sig.mark_persistence_owned(session_id)

    relay = asyncio.create_task(
        _relay_task(
            ws,
            sub,
            task_id,
            assistant_turn_id,
            debug=debug,
            store=store,
            session_id=session_id,
            runtime=runtime,
            workspace_root=workspace_root,
        )
    )
    return task_id, relay


async def _ws_loop(
    ws: WebSocket,
    auth: AuthService,
    runtime: Any,
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
    client_capabilities: dict | None = None
    while True:
        msg = await ws.receive_json()
        mtype = msg.get("type")
        if mtype == "client.capabilities":
            # Capture the client's declared capabilities (tools/protocol version).
            # Store without response; passed to next send frame.
            client_capabilities = {
                "protocolVersion": msg.get("protocolVersion", 1),
                "tools": msg.get("tools", []),
            }
        elif mtype == "send":
            # Await the previous relay if one is still running (one turn at a time).
            if relay is not None and not relay.done():
                await relay
            debug_on = (
                await store.get_debug(active_session_id or "") if active_session_id else debug_mode
            )
            active_task_id, relay = await _handle_send(
                ws,
                runtime,
                msg,
                store=store,
                active_session_id=active_session_id,
                debug=debug_on,
                client_capabilities=client_capabilities,
            )
        elif mtype == "tool.result":
            if active_task_id is not None:
                await local_tools.write_tool_result(
                    runtime.bus,
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
            # Signal cancellation so the orchestrator can detect it
            if active_task_id is not None:
                runtime.signals.request_cancel(active_task_id)
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
                runtime.signals.write_steer(active_task_id, steer_text)
            await ws.send_json({"type": "steer.ack", "taskId": active_task_id or ""})
        elif mtype == "session.new":
            mode = msg.get("mode", "chat")
            session = await store.create(title="New session", mode=mode)
            active_session_id = session["id"]
            await ws.send_json({"type": "session.updated", "session": session})
        elif mtype == "session.open":
            sid = msg.get("sessionId", "")
            session = await store.get(sid)
            if session is not None:
                active_session_id = sid
                await ws.send_json({"type": "session.updated", "session": session})
                # Replay the session's stored turns so the client can render them
                turns = await store.turns(sid)
                await ws.send_json({"type": "session.history", "sessionId": sid, "turns": turns})
        elif mtype == "session.rename":
            sid = msg.get("sessionId", "")
            title = msg.get("title", "")
            session = await store.rename(sid, title)
            if session is not None:
                await ws.send_json({"type": "session.updated", "session": session})
        elif mtype == "session.delete":
            sid = msg.get("sessionId", "")
            deleted = await store.delete(sid)
            if deleted:
                if sid == active_session_id:
                    active_session_id = None
                await ws.send_json({"type": "session.deleted", "sessionId": sid})
        elif mtype == "debug.set":
            sid = msg.get("sessionId", "") or active_session_id or ""
            enabled = bool(msg.get("enabled", False))
            debug_mode = enabled
            if sid:
                await store.set_debug(sid, enabled)
        elif mtype == "compact":
            if not active_session_id:
                await ws.send_json({"type": "compact.done", "ok": False, "error": "no_session"})
                continue
            task_id = "compact-" + uuid.uuid4().hex[:12]

            # The orchestrator's _handle special-cases kind=="compact" and
            # resolves it via self.results.set_result(task_id, ...) directly
            # (no event-stream relay for this kind), so just submit + wait.
            await runtime.submit_goal(
                {
                    "task_id": task_id,
                    "kind": "compact",
                    "session_id": active_session_id,
                    "user_id": claims["sub"],
                    "workspace_id": "",
                }
            )
            try:
                result_dict = await runtime.results.wait_result(task_id, timeout=60.0)
            except TimeoutError:
                result_dict = None

            if result_dict:
                await ws.send_json({"type": "compact.done", **result_dict})
            else:
                await ws.send_json({"type": "compact.done", "ok": False, "error": "timeout"})
        else:
            continue


def build_app(
    config: Config,
    *,
    runtime: Any,
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

    from services.orchestrator.local_store import get_local_store
    from services.ws_gateway.user_store import SqliteUserStore

    user_store = user_store or SqliteUserStore(get_local_store())
    auth = AuthService(config, user_store)
    store = session_store or _default_session_store(config)

    # default boot checks; brain check needs an http_get
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
            "memory": lambda: boot_mod.check_memory(),
            "workspace": lambda: boot_mod.check_workspace(),
        }

    app.state.auth = auth
    app.state.runtime = runtime
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
            await _ws_loop(ws, auth, runtime, boot_checks, store)
        except WebSocketDisconnect:
            return

    return app


def create_app() -> FastAPI:
    """Not a standalone entrypoint. The gateway is co-located with the orchestrator
    and needs the shared in-process runtime; run the single-process local harness:
    ``python -m services.local.main`` (which calls ``build_app(config, runtime=proc)``)."""
    raise NotImplementedError(
        "The gateway is co-located: run `python -m services.local.main` (it builds the "
        "app via build_app(config, runtime=OrchestratorProcess())). build_app needs a runtime."
    )


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "services.ws_gateway.server:create_app",
        factory=True,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8787")),
    )
