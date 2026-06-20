"""
Skill Worker — Redis Streams consumer that dispatches skill tasks.

Env vars (all optional, all have sane defaults for local/RunPod):
  REDIS_URL            redis://localhost:6379/0
  SKILLS_ROOT          path to skills/ directory
  WORKER_CONCURRENCY   max parallel in-flight tasks (default: 4)
  WORKER_ID            unique consumer name (default: hostname-pid)
  CALL_TIMEOUT         per-task skill call timeout in seconds (default: 120)
  LOG_LEVEL            info

Redis Streams contract (CLAUDE.md rule #5 — Streams, not RPUSH/BRPOP):
  Consume: XREADGROUP GROUP skill-workers <worker-id> COUNT 8 BLOCK 5000
           STREAMS labmate:skill-tasks >
  Ack:     XACK labmate:skill-tasks skill-workers <msg-id>
  Startup: XAUTOCLAIM stale pending messages (idle > 30 s) from crashed workers

Task payload (JSON, stored in the "payload" field of each stream entry):
  {
    "task_id":   "<str>",    # correlation ID
    "skill":     "<str>",    # skill directory name, e.g. "ast-repo-map"
    "tool":      "<str>",    # tool exposed by that skill, e.g. "map_repository"
    "arguments": {<dict>}    # tool arguments (validated by SkillRegistry)
  }

Result (written to Redis, expires after RESULT_TTL seconds):
  SET  labmate:result:<task_id>  <JSON>  EX 3600
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
from typing import Any

import redis.asyncio as aioredis

from services.skill_runner.skill_registry import SkillRegistry, SkillUnavailable
from services.skill_worker.manifest_loader import discover_manifests

_log = logging.getLogger("skill_worker")

STREAM = "labmate:skill-tasks"
GROUP = "skill-workers"
RESULT_PREFIX = "labmate:result:"
RESULT_TTL = 3600           # seconds
BLOCK_MS = 5_000            # XREADGROUP blocking wait
BATCH = 8                   # messages per read
PENDING_IDLE_MS = 30_000    # reclaim pending msgs idle longer than this


def _worker_id() -> str:
    explicit = os.getenv("WORKER_ID")
    return explicit or f"{socket.gethostname()}-{os.getpid()}"


class SkillWorker:
    """Redis Streams consumer that routes tasks to skill MCP subprocesses.

    Lifecycle:
        worker = SkillWorker(...)
        await worker.start()   # connects Redis, spawns skill procs, reclaims pending
        await worker.run()     # blocks until stop() is called
        await worker.stop()    # signals shutdown; run() returns after current batch
    """

    def __init__(
        self,
        redis_url: str,
        skills_root: Path | None = None,
        concurrency: int = 4,
        worker_id: str | None = None,
        call_timeout: float = 120.0,
    ) -> None:
        self._redis_url = redis_url
        self._skills_root = skills_root
        self._concurrency = concurrency
        self._worker_id = worker_id or _worker_id()
        self._call_timeout = call_timeout
        self._registry: SkillRegistry | None = None
        self._redis: aioredis.Redis | None = None
        self._sem = asyncio.Semaphore(concurrency)
        self._shutdown = asyncio.Event()

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Connect to Redis, spawn skill subprocesses, reclaim stale pending tasks."""
        pool = aioredis.ConnectionPool.from_url(
            self._redis_url,
            max_connections=self._concurrency + 4,
            decode_responses=True,
        )
        self._redis = aioredis.Redis(connection_pool=pool)

        self._registry = SkillRegistry(call_timeout=self._call_timeout)
        for manifest in discover_manifests(self._skills_root):
            try:
                await self._registry.register(manifest)
                _log.info("registered skill: %s", manifest.name)
            except Exception:
                _log.exception("failed to register %s — skipping", manifest.name)

        await self._ensure_group()
        await self._reclaim_pending()
        _log.info(
            "worker %s ready (concurrency=%d, skills=%d)",
            self._worker_id,
            self._concurrency,
            len(self._registry._skills),
        )

    async def stop(self) -> None:
        self._shutdown.set()

    async def run(self) -> None:
        """Block in the consumer loop until stop() is called."""
        assert self._redis is not None, "call start() first"
        while not self._shutdown.is_set():
            try:
                raw = await self._redis.xreadgroup(
                    groupname=GROUP,
                    consumername=self._worker_id,
                    streams={STREAM: ">"},
                    count=BATCH,
                    block=BLOCK_MS,
                )
            except aioredis.ResponseError as exc:
                _log.error("xreadgroup failed: %s", exc)
                await asyncio.sleep(1)
                continue
            if not raw:
                continue
            for _stream, entries in raw:
                for msg_id, fields in entries:
                    asyncio.create_task(self._handle(msg_id, fields))

    # ── internal ───────────────────────────────────────────────────────────

    async def _ensure_group(self) -> None:
        try:
            await self._redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
            _log.info("created consumer group '%s' on '%s'", GROUP, STREAM)
        except aioredis.ResponseError as exc:
            if "BUSYGROUP" in str(exc):
                _log.debug("consumer group '%s' already exists", GROUP)
            else:
                raise

    async def _reclaim_pending(self) -> None:
        """On startup, claim messages that were in-flight when a previous worker died."""
        start = "0-0"
        reclaimed = 0
        while True:
            next_id, entries, _ = await self._redis.xautoclaim(
                STREAM,
                GROUP,
                self._worker_id,
                min_idle_time=PENDING_IDLE_MS,
                start_id=start,
                count=BATCH,
            )
            reclaimed += len(entries)
            if next_id == "0-0" or not entries:
                break
            start = next_id
        if reclaimed:
            _log.warning("reclaimed %d stale pending message(s)", reclaimed)

    async def _handle(self, msg_id: str, fields: dict[str, str]) -> None:
        """Dispatch one task, write result, then ACK. ACK happens even on failure."""
        async with self._sem:
            task_id = msg_id  # fallback if payload missing task_id
            try:
                payload = json.loads(fields.get("payload", "{}"))
                task_id = payload.get("task_id", msg_id)
                result = await self._dispatch(payload)
                await self._write_result(task_id, result)
            except json.JSONDecodeError:
                _log.error("malformed JSON in msg %s — dropping", msg_id)
                await self._write_result(task_id, {"ok": False, "error": "malformed_payload"})
            except Exception:
                _log.exception("unhandled error for msg %s", msg_id)
                await self._write_result(task_id, {"ok": False, "error": "internal_error"})
            finally:
                await self._redis.xack(STREAM, GROUP, msg_id)

    @staticmethod
    def _jsonable(obj: Any) -> Any:
        """Make a skill result JSON-serializable.

        registry.call_tool() returns the raw MCP CallToolResult (a pydantic
        model with content blocks) — json.dumps() can't serialize it, which
        previously surfaced every successful call as "internal_error". Dump
        pydantic models to plain JSON; fall back to str() for anything else.
        """
        if hasattr(obj, "model_dump"):
            try:
                return obj.model_dump(mode="json")
            except Exception:
                return str(obj)
        try:
            json.dumps(obj)
            return obj
        except (TypeError, ValueError):
            return str(obj)

    async def _dispatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        skill = payload.get("skill", "")
        tool = payload.get("tool", "")
        arguments = payload.get("arguments", {})
        qualified = f"{skill}.{tool}"
        try:
            result = await self._registry.call_tool(qualified, arguments)
            # Check if the MCP result indicates a tool error (isError=True)
            # This is a normal return, not an exception, so we must inspect the result
            if hasattr(result, "isError") and result.isError:
                return {
                    "ok": False,
                    "error": "tool_error",
                    "result": self._jsonable(result),
                }
            return {"ok": True, "result": self._jsonable(result)}
        except SkillUnavailable as exc:
            return {"ok": False, "error": "skill_unavailable", "detail": str(exc)}
        except Exception as exc:
            _log.error("dispatch error for %s: %r", qualified, exc)
            return {"ok": False, "error": "dispatch_failed", "detail": str(exc)}

    async def _write_result(self, task_id: str, result: dict[str, Any]) -> None:
        key = f"{RESULT_PREFIX}{task_id}"
        await self._redis.set(key, json.dumps(result), ex=RESULT_TTL)
        await self._redis.publish(key, "ready")


def _setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "info").upper()
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )


async def _async_main() -> None:
    _setup_logging()
    skills_root_env = os.getenv("SKILLS_ROOT", "")
    worker = SkillWorker(
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        skills_root=Path(skills_root_env) if skills_root_env else None,
        concurrency=int(os.getenv("WORKER_CONCURRENCY", "4")),
        worker_id=os.getenv("WORKER_ID"),
        call_timeout=float(os.getenv("CALL_TIMEOUT", "120")),
    )

    loop = asyncio.get_running_loop()

    def _on_signal(sig: int) -> None:
        _log.info("received signal %d — shutting down", sig)
        asyncio.create_task(worker.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _on_signal, sig)

    await worker.start()
    await worker.run()


if __name__ == "__main__":
    asyncio.run(_async_main())
