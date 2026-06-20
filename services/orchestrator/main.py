"""
Orchestrator process entrypoint.

Bootstraps in order:
  1. Logging (stderr only)
  2. StorageManager  (MongoDB + Chroma + Redis — reads MONGO_URI / CHROMA_URL / REDIS_URL)
  3. MCPClientManager (spawns MCP bridge subprocess, waits for ready)
  4. LangGraph       (build_graph with MongoDBSaver checkpointer)
  5. Goal loop       (XREADGROUP labmate:goals → run_task → write result)

Env vars:
  MONGO_URI          mongodb://localhost:27017/labmate
  CHROMA_URL         http://localhost:8765          (RunPod) | http://chroma:8000 (Docker)
  REDIS_URL          redis://localhost:6379/0
  GEMMA_BASE         http://localhost:8000/v1
  QWEN_BASE          (defaults to GEMMA_BASE on single-GPU)
  MCP_BRIDGE_CMD     node
  MCP_BRIDGE_ARGS    /path/to/mcp-bridge/dist/index.js
  WORKSPACE_PATH     /workspace
  SANDBOX_CONTAINER  labmate-sandbox   (Docker container name for code execution)
  LOG_LEVEL          info

Redis Streams contract (CLAUDE.md rule #5):
  Consume: XREADGROUP GROUP orchestrators <worker-id> COUNT 1 BLOCK 5000
           STREAMS labmate:goals >
  Ack:     XACK labmate:goals orchestrators <msg-id>

Goal payload (JSON in the "payload" field):
  { "task_id": "<str>", "task": "<str>", "session_id": "<str|null>", "user_id": "<str|null>", "workspace_id": "<str|null>" }

Result (24 h TTL):
  SET  labmate:result:<task_id>  <JSON>  EX 86400
  PUBLISH  labmate:result:<task_id>  "ready"
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import sys
from pathlib import Path

import redis.asyncio as aioredis
from mcp import StdioServerParameters

from services.orchestrator.graph import build_graph, GEMMA_BASE, QWEN_BASE
from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator
from services.orchestrator.storage_manager import StorageManager
from services.orchestrator.mcp_client_manager import MCPClientManager
from services.orchestrator.skill_router import SkillRouter
from services.orchestrator import events
from services.skill_runner.skill_runner import SkillRunner

_log = logging.getLogger("orchestrator")

GOALS_STREAM = "labmate:goals"
GOALS_GROUP  = "orchestrators"
RESULT_PREFIX = "labmate:result:"
RESULT_TTL    = 86_400    # 24 h
BLOCK_MS      = 5_000


def _worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


def _build_mcp_params() -> StdioServerParameters:
    cmd = os.getenv("MCP_BRIDGE_CMD", "node")
    default_js = str(
        Path(__file__).resolve().parent.parent / "mcp-bridge" / "dist" / "index.js"
    )
    args_str = os.getenv("MCP_BRIDGE_ARGS", default_js)
    return StdioServerParameters(command=cmd, args=[args_str])


class OrchestratorProcess:
    """Owns the full lifecycle of all services and the goal-processing loop."""

    def __init__(self) -> None:
        self._worker_id = _worker_id()
        self._shutdown  = asyncio.Event()
        self._redis: aioredis.Redis | None = None
        self._mcp:   MCPClientManager | None = None

    # ── top-level run ──────────────────────────────────────────────────────

    async def run(self) -> None:
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        workspace  = os.getenv("WORKSPACE_PATH", "/workspace")

        async with StorageManager() as _sm:
            _log.info("storage ready")

            self._mcp = MCPClientManager(_build_mcp_params())
            await self._mcp.start()
            try:
                await self._mcp.wait_ready(timeout=30.0)
                _log.info("MCP bridge ready (%d tools)", len(self._mcp.tools))
            except asyncio.TimeoutError:
                _log.warning("MCP bridge did not become ready within 30 s — continuing")

            # Note: skill_router is built below, so we'll update async_orch later
            async_orch = AsyncOrchestrator(
                qwen_api_base=QWEN_BASE,
                gemma_api_base=GEMMA_BASE,
            )

            # Initialize Redis BEFORE skill router (CLAUDE.md: "after self._redis is created")
            pool = aioredis.ConnectionPool.from_url(
                redis_url, max_connections=8, decode_responses=True,
            )
            self._redis = aioredis.Redis(connection_pool=pool)
            await self._ensure_group()

            # Build skill router (optional, wrapped in try/except so startup never fails)
            skill_router = None
            try:
                skills_root = Path(__file__).resolve().parent.parent / "skills"
                runner = SkillRunner(roots=[skills_root])
                runner.discover()
                skill_router = SkillRouter(
                    runner=runner,
                    redis=self._redis,
                    gemma_api_base=GEMMA_BASE,
                )
                # Wire skill_router into async_orch for react_execute
                async_orch.skill_router = skill_router
                async_orch.mcp = self._mcp
                async_orch.workspace = workspace
                _log.info("skill router ready (%d skills)", len(runner.catalog))
            except Exception:
                _log.warning("failed to initialize skill router — continuing without skills", exc_info=True)

            # CodingOrchestrator and build_graph have a circular dependency:
            # build_graph(orch, ...) closes node functions over the orch object;
            # CodingOrchestrator(graph, ...) stores the compiled graph.
            # Resolution: create orch with graph=None, compile graph, then set it.
            orch = CodingOrchestrator(
                graph=None,
                workspace_path=workspace,
                docker_container=os.getenv("SANDBOX_CONTAINER", ""),
                gemma_api_base=GEMMA_BASE,
                qwen_api_base=QWEN_BASE,
                mcp=self._mcp,
                skill_router=skill_router,
            )
            graph, _cp = build_graph(
                orch=orch,
                async_orch=async_orch,
                mongo_uri=os.getenv("MONGO_URI", "mongodb://localhost:27017/labmate"),
            )
            orch.graph = graph

            _log.info("orchestrator %s ready", self._worker_id)
            await self._loop(orch, _sm)

        if self._mcp:
            await self._mcp.shutdown()

    async def stop(self) -> None:
        self._shutdown.set()

    # ── goal loop ──────────────────────────────────────────────────────────

    async def _loop(self, orch: CodingOrchestrator, storage: StorageManager) -> None:
        while not self._shutdown.is_set():
            try:
                raw = await self._redis.xreadgroup(
                    groupname=GOALS_GROUP,
                    consumername=self._worker_id,
                    streams={GOALS_STREAM: ">"},
                    count=1,
                    block=BLOCK_MS,
                )
            except (aioredis.TimeoutError, TimeoutError):
                # Defensive: a blocking xreadgroup should tolerate a read-timeout
                # and re-poll. This does NOT fire on the pinned redis-py 5.x
                # (which returns [] cleanly when BLOCK elapses), but redis-py
                # 8.x regressed blocking-read handling and raises TimeoutError
                # here under a busy event loop — which, if uncaught, silently
                # kills goal consumption. Keep the catch regardless of version.
                continue
            except aioredis.ResponseError as exc:
                _log.error("xreadgroup error: %s", exc)
                await asyncio.sleep(1)
                continue

            if not raw:
                continue

            for _stream, entries in raw:
                for msg_id, fields in entries:
                    await self._handle(msg_id, fields, orch, storage)

    async def _handle(
        self,
        msg_id: str,
        fields: dict[str, str],
        orch: CodingOrchestrator,
        storage: StorageManager,
    ) -> None:
        # Safe defaults — must be bound before try so finally never raises NameError
        task_id = msg_id
        user_id = ""
        workspace_id = ""
        session_id = ""
        task_text = ""
        final_state = {}
        task_succeeded = False
        _emitter: events.EventEmitter | None = None
        _token = None

        try:
            payload    = json.loads(fields.get("payload", "{}"))
            task_id    = payload.get("task_id", msg_id)
            task_text  = payload.get("task", "")
            session_id = payload.get("session_id") or task_id
            user_id    = payload.get("user_id", "")
            workspace_id = payload.get("workspace_id", "")

            _emitter = events.EventEmitter(self._redis, task_id)
            _token = events.current_emitter.set(_emitter)
            await _emitter.emit("turn.start", task=task_text)

            # Fix 2: Record session if user_id and workspace_id are present
            if user_id and workspace_id:
                from .models import SessionMeta
                try:
                    await storage.workspaces.record_session(SessionMeta(
                        session_id=session_id,
                        user_id=user_id,
                        workspace_id=workspace_id,
                        task_preview=task_text[:120],
                    ))
                except Exception:
                    pass  # never let session recording block task execution

            # Fix 3: Upsert workspace on first sight
            if user_id and workspace_id:
                try:
                    await storage.workspaces.upsert_workspace(workspace_id, user_id)
                except Exception:
                    pass  # upsert failure never blocks task

            _log.info("task %s: %.80s", task_id, task_text)
            final_state = await orch.run_task(
                task_text, session_id, user_id=user_id, workspace_id=workspace_id
            )
            task_succeeded = True
            # Stream the final answer with typewriter effect (answer.delta + answer.done)
            if hasattr(orch, "stream_final_answer"):
                try:
                    streamed = await orch.stream_final_answer(task_text, final_state)
                    if isinstance(final_state, dict) and streamed:
                        final_state["final_answer"] = streamed
                except Exception:
                    pass  # best-effort; never let streaming block the result
            # Derive ok from final_state.error (FIX #2: failed subtasks now finalize with error set, not exception)
            ok_flag = final_state.get("error") is None
            await self._write_result(task_id, {"ok": ok_flag, "state": final_state})
            _log.info("task %s complete", task_id)

        except Exception:
            _log.exception("task %s failed", task_id)
            await self._write_result(task_id, {"ok": False, "error": "task_failed"})
        finally:
            # Fix 2: Complete session in finally block
            if user_id and workspace_id:
                try:
                    # Check if error value is None (not just string presence)
                    if not task_succeeded:
                        ok_flag = False
                    elif isinstance(final_state, dict):
                        ok_flag = final_state.get("error") is None
                    else:
                        ok_flag = True
                    await storage.workspaces.complete_session(session_id, ok=ok_flag)
                except Exception:
                    pass
            try:
                _status = "complete" if task_succeeded and (
                    not isinstance(final_state, dict) or final_state.get("error") is None
                ) else "error"
                _answer = final_state.get("final_answer", "") if isinstance(final_state, dict) else ""
                await events.emit("turn.done", status=_status, final_answer=_answer)
            except Exception:
                pass
            if _token is not None:
                events.current_emitter.reset(_token)
            await self._redis.xack(GOALS_STREAM, GOALS_GROUP, msg_id)

    async def _write_result(self, task_id: str, result: dict) -> None:
        key = f"{RESULT_PREFIX}{task_id}"
        await self._redis.set(key, json.dumps(result, default=str), ex=RESULT_TTL)
        await self._redis.publish(key, "ready")

    async def _ensure_group(self) -> None:
        try:
            await self._redis.xgroup_create(
                GOALS_STREAM, GOALS_GROUP, id="0", mkstream=True,
            )
        except aioredis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise


def _setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "info").upper()
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )


async def _async_main() -> None:
    _setup_logging()
    proc = OrchestratorProcess()

    loop = asyncio.get_running_loop()

    def _on_signal(sig: int) -> None:
        _log.info("signal %d — shutting down", sig)
        asyncio.create_task(proc.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _on_signal, sig)

    await proc.run()


if __name__ == "__main__":
    asyncio.run(_async_main())
