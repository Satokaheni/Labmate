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

            async_orch = AsyncOrchestrator(
                qwen_api_base=QWEN_BASE,
                gemma_api_base=GEMMA_BASE,
            )

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
            )
            graph, _cp = build_graph(
                orch=orch,
                async_orch=async_orch,
                mongo_uri=os.getenv("MONGO_URI", "mongodb://localhost:27017/labmate"),
            )
            orch.graph = graph

            pool = aioredis.ConnectionPool.from_url(
                redis_url, max_connections=8, decode_responses=True,
            )
            self._redis = aioredis.Redis(connection_pool=pool)
            await self._ensure_group()

            _log.info("orchestrator %s ready", self._worker_id)
            await self._loop(orch)

        if self._mcp:
            await self._mcp.shutdown()

    async def stop(self) -> None:
        self._shutdown.set()

    # ── goal loop ──────────────────────────────────────────────────────────

    async def _loop(self, orch: CodingOrchestrator) -> None:
        while not self._shutdown.is_set():
            try:
                raw = await self._redis.xreadgroup(
                    groupname=GOALS_GROUP,
                    consumername=self._worker_id,
                    streams={GOALS_STREAM: ">"},
                    count=1,
                    block=BLOCK_MS,
                )
            except aioredis.ResponseError as exc:
                _log.error("xreadgroup error: %s", exc)
                await asyncio.sleep(1)
                continue

            if not raw:
                continue

            for _stream, entries in raw:
                for msg_id, fields in entries:
                    await self._handle(msg_id, fields, orch)

    async def _handle(
        self,
        msg_id: str,
        fields: dict[str, str],
        orch: CodingOrchestrator,
    ) -> None:
        task_id = msg_id
        try:
            payload    = json.loads(fields.get("payload", "{}"))
            task_id    = payload.get("task_id", msg_id)
            task_text  = payload.get("task", "")
            session_id = payload.get("session_id") or task_id
            user_id    = payload.get("user_id", "")
            workspace_id = payload.get("workspace_id", "")

            _log.info("task %s: %.80s", task_id, task_text)
            final_state = await orch.run_task(
                task_text, session_id, user_id=user_id, workspace_id=workspace_id
            )
            await self._write_result(task_id, {"ok": True, "state": final_state})
            _log.info("task %s complete", task_id)

        except Exception:
            _log.exception("task %s failed", task_id)
            await self._write_result(task_id, {"ok": False, "error": "task_failed"})
        finally:
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
