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
import re as _re
import signal
import socket
import sys
from pathlib import Path

import redis.asyncio as aioredis
from mcp import StdioServerParameters

from services.orchestrator import call_counter, client_context, events, skill_curator
from services.orchestrator.coding_orchestrator import AsyncOrchestrator, CodingOrchestrator
from services.orchestrator.completion_guard import reconcile_final_answer
from services.orchestrator.graph import GEMMA_BASE, QWEN_BASE, build_graph
from services.orchestrator.mcp_client_manager import MCPClientManager
from services.orchestrator.memory_search import MemorySearch
from services.orchestrator.skill_curator import (
    CapturedSequence,
    RecentSequences,
    run_curator,
)
from services.orchestrator.skill_router import SkillRouter
from services.orchestrator.storage_manager import StorageManager
from services.orchestrator.tool_manifest import parse_manifest
from services.skill_runner.skill_runner import SkillRunner

_log = logging.getLogger("orchestrator")

GOALS_STREAM = "labmate:goals"
GOALS_GROUP = "orchestrators"
RESULT_PREFIX = "labmate:result:"
RESULT_TTL = 86_400  # 24 h
BLOCK_MS = 5_000

# Per-request context window the model actually serves = llama-server --ctx-size
# DIVIDED BY --parallel (each slot gets ctx-size/n_parallel tokens). serve-model.sh
# defaults to --ctx-size 262144 --parallel 2 → 131072 per request. This ceiling drives
# the context gauge (used/free shown to the frontend) and the auto-compact thresholds,
# so it must equal the PER-SLOT window, not the raw --ctx-size. Keep CTX_WINDOW in sync
# with serve-model.sh whenever the slot math changes.
CTX_TOKENS = int(os.getenv("CTX_WINDOW", "131072"))
MICRO_THRESH = int(CTX_TOKENS * 0.70)
FULL_THRESH = int(CTX_TOKENS * 0.85)

# How often, during a running turn, to re-emit the context-window telemetry so the
# frontend strip climbs live instead of only updating once at turn end. 0 disables
# the live refresher (a single emit at turn start + one at turn end still fire).
CONTEXT_REFRESH_S = float(os.getenv("CONTEXT_REFRESH_SECONDS", "2.0"))


def _context_window(used_fallback: int = 0) -> dict:
    """Build the context-window telemetry payload from the live token high-water mark.

    `used` is the peak prompt_tokens seen across THIS task's LLM calls. The per-task
    counter resets each turn, so before this turn's first model call we fall back to
    `used_fallback` — the carried fill from the prior turn in this session — so the
    strip doesn't drop to 0% at the start of every new message. Once a real peak is
    recorded it wins (so the value still reflects compaction, which shrinks the prompt).
    The whole measured fill is attributed to `conversation` — the dominant real
    component — since usage data carries no per-segment breakdown.
    """
    ctx_max = CTX_TOKENS
    _peak = call_counter.get_peak_prompt_tokens()
    ctx_used = min(_peak if _peak > 0 else max(used_fallback, 0), ctx_max)
    return {
        "max": ctx_max,
        "used": ctx_used,
        "free": max(0, ctx_max - ctx_used),
        "segments": {
            "systemPrompt": 0,
            "skillInstructions": 0,
            "conversation": ctx_used,
            "workingMemory": 0,
            "reasoning": 0,
        },
    }


async def _context_refresh_loop(
    emitter: events.EventEmitter,
    interval_s: float = CONTEXT_REFRESH_S,
    used_fallback: int = 0,
) -> None:
    """Re-emit the context window every `interval_s` until cancelled at turn end.

    Created with asyncio.create_task AFTER the per-task counter contextvar is set,
    so it inherits that context and reports the live peak. `used_fallback` carries
    the prior turn's fill so the strip shows it until this turn's first model call.
    Best-effort: a failed emit is swallowed; cancellation ends the loop cleanly.
    """
    try:
        while True:
            await asyncio.sleep(interval_s)
            try:
                await emitter.emit("context", window=_context_window(used_fallback))
            except Exception:
                pass
    except asyncio.CancelledError:
        pass


# Background proactive compaction: how often the sweeper wakes, and the cap on
# sessions inspected per sweep (newest-active first) so one sweep stays bounded.
BG_COMPACT_INTERVAL_S = int(os.getenv("BG_COMPACT_INTERVAL_S", "120"))
BG_COMPACT_MAX_SESSIONS = int(os.getenv("BG_COMPACT_MAX_SESSIONS", "20"))


def _extract_tool_sequence(final_state) -> tuple[str, ...]:
    """Pull the ordered tool/skill names a completed goal used from its state.

    Reads final_state['tools_used'] when present (the orchestrator records the
    per-goal tool sequence there). Returns an empty tuple for a non-dict or a
    state with no recorded tools, so the curator simply has nothing to draft.
    """
    if not isinstance(final_state, dict):
        return ()
    tools = final_state.get("tools_used") or []
    if not isinstance(tools, list | tuple):
        return ()
    return tuple(str(t) for t in tools if t)


def _slug_for(task_text: str) -> str:
    """kebab-case candidate skill name from the goal text (deterministic)."""
    words = _re.findall(r"[a-z0-9]+", (task_text or "").lower())
    slug = "-".join(words[:4]) or "proposed-skill"
    return slug[:48]


def _worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


def _build_mcp_params() -> StdioServerParameters:
    cmd = os.getenv("MCP_BRIDGE_CMD", "node")
    default_js = str(Path(__file__).resolve().parent.parent / "mcp-bridge" / "dist" / "index.js")
    args_str = os.getenv("MCP_BRIDGE_ARGS", default_js)
    return StdioServerParameters(command=cmd, args=[args_str])


def _build_codegraph_params() -> StdioServerParameters:
    return StdioServerParameters(
        command="python",
        args=["-m", "services.codegraph_embedder.server"],
        env={**os.environ},
    )


class OrchestratorProcess:
    """Owns the full lifecycle of all services and the goal-processing loop."""

    def __init__(self) -> None:
        self._worker_id = _worker_id()
        self._shutdown = asyncio.Event()
        self._redis: aioredis.Redis | None = None
        self._mcp: MCPClientManager | None = None
        self._codegraph_mcp: MCPClientManager | None = None
        self._recent_sequences = RecentSequences()
        self._last_goal_at: float = 0.0
        # Per-session carried context fill (peak prompt tokens of the last turn), so
        # the context-window strip doesn't reset to 0% at the start of each new message.
        self._session_ctx_fill: dict[str, int] = {}

    # ── top-level run ──────────────────────────────────────────────────────

    async def run(self) -> None:
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        workspace = os.getenv("WORKSPACE_PATH", "/workspace")

        async with StorageManager() as _sm:
            _log.info("storage ready")

            self._mcp = MCPClientManager(_build_mcp_params())
            await self._mcp.start()
            try:
                await self._mcp.wait_ready(timeout=30.0)
                _log.info("MCP bridge ready (%d tools)", len(self._mcp.tools))
            except TimeoutError:
                _log.warning("MCP bridge did not become ready within 30 s — continuing")

            self._codegraph_mcp = MCPClientManager(_build_codegraph_params())
            await self._codegraph_mcp.start()
            try:
                await self._codegraph_mcp.wait_ready(timeout=120.0)
                _log.info(
                    "codegraph semantic search ready (%d tools)", len(self._codegraph_mcp.tools)
                )
            except TimeoutError:
                _log.warning(
                    "codegraph MCP did not become ready within 120 s (index still building?) — continuing"
                )

            # Note: skill_router is built below, so we'll update async_orch later
            async_orch = AsyncOrchestrator(
                qwen_api_base=QWEN_BASE,
                gemma_api_base=GEMMA_BASE,
            )

            # Wire the durable inner-loop checkpoint store when a Mongo-backed
            # StorageManager is available. No-op when checkpointing is disabled
            # (the flag is read inside _run_react_loop) or no storage exists.
            try:
                from .loop_checkpoint import CheckpointStore

                async_orch.checkpoint_store = CheckpointStore(_sm.loop_checkpoint_collection)
            except Exception:  # best-effort wiring; never block startup
                async_orch.checkpoint_store = None

            # Initialize Redis BEFORE skill router (CLAUDE.md: "after self._redis is created")
            pool = aioredis.ConnectionPool.from_url(
                redis_url,
                max_connections=8,
                decode_responses=True,
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
                _log.warning(
                    "failed to initialize skill router — continuing without skills", exc_info=True
                )

            # Wire redis and codegraph_mcp outside the try/except (always available)
            async_orch.redis = self._redis
            async_orch.codegraph_mcp = self._codegraph_mcp
            async_orch.memory_search = MemorySearch(_sm)

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

            bg_compactor = asyncio.create_task(
                self._background_compactor(orch, _sm),
                name="background-compactor",
            )
            bg_curator = None
            if skill_curator.ENABLE_SKILL_CURATOR:
                bg_curator = asyncio.create_task(
                    self._background_curator(orch),
                    name="background-curator",
                )
                _log.info("skill curator enabled (proposal-only, background)")
            try:
                await self._loop(orch, _sm)
            finally:
                bg_compactor.cancel()
                try:
                    await bg_compactor
                except asyncio.CancelledError:
                    pass
                if bg_curator is not None:
                    bg_curator.cancel()
                    try:
                        await bg_curator
                    except asyncio.CancelledError:
                        pass

        if self._mcp:
            await self._mcp.shutdown()
        if self._codegraph_mcp:
            await self._codegraph_mcp.shutdown()

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

    async def _background_compactor(
        self,
        orch: CodingOrchestrator,
        storage: StorageManager,
    ) -> None:
        """Periodically compact idle, high-fill sessions so the next message has room.

        Runs as its own asyncio task next to the goal loop. Each gate (idle + fill)
        lives in ContextManager.maybe_background_compact; this method only finds
        candidate sessions and dispatches. Best-effort: one session's failure never
        stops the sweep, and the sweep never blocks goal handling.
        """

        async def _bg_llm(p: str) -> str:
            import litellm as _litellm

            r = await _litellm.acompletion(
                model="openai/gemma-4-31b",
                api_base=orch._gemma_base,
                api_key="not-needed",
                messages=[{"role": "user", "content": p}],
                extra_body={"thinking_budget_tokens": 0},
            )
            return r.choices[0].message.content

        while not self._shutdown.is_set():
            try:
                await asyncio.sleep(BG_COMPACT_INTERVAL_S)
                if self._shutdown.is_set():
                    break

                session_ids = await storage._db["messages"].distinct("session_id")
                for session_id in session_ids[:BG_COMPACT_MAX_SESSIONS]:
                    if self._shutdown.is_set():
                        break
                    if not session_id:
                        continue
                    result = await storage.context_manager.maybe_background_compact(
                        session_id,
                        _bg_llm,
                    )
                    if result and result.get("reflections"):
                        asyncio.create_task(
                            storage.consolidator.write_reflections(
                                session_id, result["reflections"]
                            )
                        )
                    if result:
                        _log.info(
                            "background compact: session %s pruned %d messages",
                            session_id,
                            result.get("pruned_messages", 0),
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.warning("background compactor sweep failed (non-fatal)", exc_info=True)

    async def _background_curator(self, orch: CodingOrchestrator) -> None:
        """Periodic, best-effort skill-curator loop (mirrors _background_compactor).

        Only created when ENABLE_SKILL_CURATOR is set. Each cycle is gated by
        should_run_now (interval + idle) inside run_curator. Failures are isolated
        there; this loop additionally guards its own sleep/iteration.
        """
        import time as _time

        skills_root = Path(__file__).resolve().parent.parent / "skills"
        state_path = Path(os.getenv("CURATOR_STATE_DIR", ".data")) / "skill_curator.json"
        # Wake roughly hourly; the real interval gate lives in run_curator.
        wake_s = int(os.getenv("CURATOR_WAKE_INTERVAL_S", "3600"))

        async def _draft(prompt: str) -> str:
            # The ONE LLM call — routed through architect() (api_key + budget set).
            return await orch.architect(prompt, thinking_budget=0)

        while not self._shutdown.is_set():
            try:
                await asyncio.sleep(wake_s)
                if self._shutdown.is_set():
                    break
                now = _time.time()
                idle_for_s = now - self._last_goal_at if self._last_goal_at else 1e9
                await run_curator(
                    skills_root=skills_root,
                    state_path=state_path,
                    recent=self._recent_sequences,
                    draft_fn=_draft,
                    now=now,
                    idle_for_s=idle_for_s,
                    interval_hours=skill_curator.CURATOR_INTERVAL_HOURS,
                    min_idle_hours=skill_curator.CURATOR_MIN_IDLE_HOURS,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.debug("background curator sweep failed (non-fatal)", exc_info=True)

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
        _counter_token = None
        _manifest_token = None
        _ws_root_token = None
        _ctx_task: asyncio.Task | None = None

        try:
            payload = json.loads(fields.get("payload", "{}"))
            task_id = payload.get("task_id", msg_id)
            task_text = payload.get("task", "")
            session_id = payload.get("session_id") or task_id
            user_id = payload.get("user_id", "")
            workspace_id = payload.get("workspace_id", "")
            kind = payload.get("kind", "task")

            # User-triggered compact — no graph run needed
            if kind == "compact":
                _gemma_base = orch._gemma_base

                async def _compact_llm(p: str) -> str:
                    import litellm as _litellm

                    r = await _litellm.acompletion(
                        model="openai/gemma-4-31b",
                        api_base=_gemma_base,
                        api_key="not-needed",
                        messages=[{"role": "user", "content": p}],
                        extra_body={"thinking_budget_tokens": 0},
                    )
                    return r.choices[0].message.content

                result = await storage.context_manager.full_compact(
                    session_id,
                    llm_fn=_compact_llm,
                )
                # Write reflections extracted during compaction to semantic memory
                if result.get("reflections"):
                    asyncio.create_task(
                        storage.consolidator.write_reflections(session_id, result["reflections"])
                    )
                await self._write_result(task_id, {"ok": True, **result})
                return

            _emitter = events.EventEmitter(self._redis, task_id)
            _token = events.current_emitter.set(_emitter)
            # Per-task LLM call counter (A/B instrumentation): set a fresh counter for
            # this task's context; the litellm success callback increments it.
            _counter_token = call_counter.start()
            # Per-task client capability manifest: parse from payload and set in context.
            # When no manifest is present, behavior is unchanged (full tool list).
            _parsed_manifest = parse_manifest(payload.get("client_capabilities"))
            _manifest_token = client_context.set_manifest(_parsed_manifest)
            # DIAGNOSTIC (P2-B.1): log what the orchestrator actually received so a
            # doc-skill that never reaches the catalog can be localized. Names only.
            try:
                from services.orchestrator.tool_manifest import client_doc_skills

                _mtools = (_parsed_manifest or {}).get("tools", []) if _parsed_manifest else []
                _doc = sorted(client_doc_skills(_parsed_manifest).keys())
                _log.info(
                    "manifest-diag: present=%s tool_count=%d doc_skills=%s",
                    _parsed_manifest is not None,
                    len(_mtools),
                    _doc,
                )
            except Exception as _diag_exc:  # never let diagnostics break a task
                _log.warning("manifest-diag failed: %s", _diag_exc)
            # Per-task workspace root: set in context for skills that need absolute paths.
            _ws_root_token = client_context.set_workspace_root(
                payload.get("workspace_root") or None
            )

            # Emit active status so the frontend agent indicator lights up
            await _emitter.emit(
                "agent_status",
                status={
                    "brain": {
                        "model": os.getenv("BRAIN_MODEL", "gemma-31b"),
                        "endpoint": os.getenv("GEMMA_BASE", "http://localhost:8000/v1"),
                        "state": "active",
                        "node": "plan_node",
                        "thinkingBudget": 3000,
                    },
                    "nervousSystem": {
                        "name": "MCP bridge",
                        "transport": "stdio",
                        "state": "connected",
                        "toolsRegistered": 0,
                    },
                    "hands": {"skills": []},
                },
            )
            await _emitter.emit("turn.start", task=task_text)
            # Populate the strip immediately with the fill CARRIED from the prior turn
            # in this session, so it shows the accumulated context (e.g. 11%) instead of
            # resetting to 0% — then refresh live as this turn's real peak is measured.
            _carried_fill = self._session_ctx_fill.get(session_id, 0)
            _log.info("ctx-carry: session=%s carried=%d", session_id, _carried_fill)
            await _emitter.emit("context", window=_context_window(_carried_fill))
            if CONTEXT_REFRESH_S > 0:
                _ctx_task = asyncio.create_task(
                    _context_refresh_loop(_emitter, used_fallback=_carried_fill)
                )

            # Fix 2: Record session if user_id and workspace_id are present
            if user_id and workspace_id:
                from .models import SessionMeta

                try:
                    await storage.workspaces.record_session(
                        SessionMeta(
                            session_id=session_id,
                            user_id=user_id,
                            workspace_id=workspace_id,
                            task_preview=task_text[:120],
                        )
                    )
                except Exception:
                    pass  # never let session recording block task execution

            # Fix 3: Upsert workspace on first sight
            if user_id and workspace_id:
                try:
                    await storage.workspaces.upsert_workspace(workspace_id, user_id)
                except Exception:
                    pass  # upsert failure never blocks task

            # Load AGENT.md (or workspace.instructions DB field) — pinned for this session
            agent_instructions = ""
            try:
                agent_instructions = await storage.workspaces.load_agent_instructions(workspace_id)
            except Exception:
                pass

            # Auto-compact: check context fill before running the graph
            try:
                ctx_check = await storage.context_manager.build_context(
                    session_id=session_id,
                    current_task=task_text,
                    system_prompt="",
                    agent_instructions=agent_instructions,
                )
                if ctx_check.total_tokens >= FULL_THRESH:
                    compact_result = await storage.context_manager.full_compact(
                        session_id,
                        llm_fn=lambda p: orch.architect(p, thinking_budget=0),
                    )
                    await events.emit("compact.auto", freed=compact_result["pruned_messages"])
                    _log.info(
                        "task %s: auto full-compact freed %d messages",
                        task_id,
                        compact_result["pruned_messages"],
                    )
                    if compact_result.get("reflections"):
                        asyncio.create_task(
                            storage.consolidator.write_reflections(
                                session_id, compact_result["reflections"]
                            )
                        )
                elif ctx_check.total_tokens >= MICRO_THRESH:
                    freed = await storage.context_manager.microcompact(session_id)
                    if freed:
                        await events.emit("compact.micro", freed=freed)
                        _log.info("task %s: microcompact freed %d chars", task_id, freed)
            except Exception as exc:
                _log.warning("auto-compact check failed (non-fatal): %s", exc)

            # Importance-based decay sweep (at most once/hour per session)
            try:
                if await storage.cache_get(f"decay_swept:{session_id}") is None:
                    asyncio.create_task(storage.decay_expired_memories(session_id))
                    await storage.cache_set(f"decay_swept:{session_id}", "1", ttl=3600)
            except Exception:
                pass  # decay is best-effort; never block task execution

            _log.info("task %s: %.80s", task_id, task_text)
            final_state = await orch.run_task(
                task_text,
                session_id,
                user_id=user_id,
                workspace_id=workspace_id,
                agent_instructions=agent_instructions,
            )
            task_succeeded = True
            # If the graph halted for clarification, surface the question — do NOT
            # call stream_final_answer (it would guess an answer to the ambiguous task).
            if isinstance(final_state, dict) and final_state.get("awaiting_clarification"):
                _question = final_state.get("clarification_question", "")
                final_state["final_answer"] = _question
                try:
                    await events.emit("answer.delta", text=_question)
                    await events.emit("answer.done", text=_question)
                except Exception:
                    pass  # best-effort; never let event emission block the result
            # FIX 10: direct-answer fast-path — the plan node already produced final_answer,
            # so do NOT call stream_final_answer (it would re-ask the model to re-answer).
            # Surface the existing final_answer via the same answer.delta/answer.done events.
            elif isinstance(final_state, dict) and final_state.get("direct_answer"):
                _answer = final_state.get("final_answer", "")
                try:
                    await events.emit("answer.delta", text=_answer)
                    await events.emit("answer.done", text=_answer)
                except Exception:
                    pass  # best-effort; never let event emission block the result
            # Otherwise stream the final answer with typewriter effect (answer.delta + answer.done)
            elif hasattr(orch, "stream_final_answer"):
                try:
                    streamed = await orch.stream_final_answer(task_text, final_state)
                    if isinstance(final_state, dict) and streamed:
                        final_state["final_answer"] = streamed
                except Exception:
                    pass  # best-effort; never let streaming block the result
            # Reconcile the RENDERED final answer (post-summarizer) with ok/error.
            # The skill-first and ReAct seams reconcile their immediate outputs, but
            # the user-facing punt wording is produced downstream by the summarizer /
            # stream_final_answer and was never re-checked — so a "file too large /
            # provide a snippet" answer slipped through as ok=True (A/B report §8.3).
            # Setting final_state["error"] here propagates the downgrade to ok_flag
            # below, the stored result payload, the finally-block status, and
            # complete_session. Clarification states never reach here with a punt.
            if isinstance(final_state, dict) and not final_state.get("awaiting_clarification"):
                _rendered = final_state.get("final_answer", "") or ""
                _recon_ok, _recon_err, _ = reconcile_final_answer(
                    final_state.get("error") is None,
                    final_state.get("error"),
                    _rendered,
                    tests_passed=bool(final_state.get("tests_passed", False)),
                )
                if not _recon_ok and final_state.get("error") is None:
                    final_state["error"] = _recon_err
            # Derive ok from final_state.error (FIX #2: failed subtasks now finalize with error set, not exception)
            ok_flag = final_state.get("error") is None
            # A/B instrumentation: llm_calls is the approximate per-task count of
            # successful litellm completions (see call_counter.py for exactly what it counts).
            await self._write_result(
                task_id,
                {
                    "ok": ok_flag,
                    "state": final_state,
                    "llm_calls": call_counter.get_count(),
                },
            )
            _log.info("task %s complete", task_id)
            # Skill-curator: record a SUCCESSFUL multi-tool sequence as a draft
            # candidate (best-effort; never blocks). RecentSequences itself drops
            # failed / too-short sequences.
            import time as _time

            self._last_goal_at = _time.time()
            try:
                _tools = _extract_tool_sequence(final_state)
                self._recent_sequences.record(
                    CapturedSequence(
                        name=_slug_for(task_text),
                        goal=task_text[:500],
                        tools=_tools,
                        ok=ok_flag,
                        ts=self._last_goal_at,
                    )
                )
            except Exception:
                pass  # capture is best-effort
            # Task-boundary reflection: write to semantic memory asynchronously
            _final_answer = (
                final_state.get("final_answer", "") if isinstance(final_state, dict) else ""
            )
            asyncio.create_task(
                storage.consolidator.on_task_complete(
                    session_id=session_id,
                    goal=task_text[:500],
                    success=ok_flag,
                    summary=_final_answer[:1_000],
                )
            )

        except Exception:
            _log.exception("task %s failed", task_id)
            await self._write_result(
                task_id,
                {
                    "ok": False,
                    "error": "task_failed",
                    "llm_calls": call_counter.get_count(),
                },
            )
            # Failure reflection: always write — highest signal for learning
            asyncio.create_task(
                storage.consolidator.on_task_complete(
                    session_id=session_id,
                    goal=task_text[:500],
                    success=False,
                    summary="Task raised an unhandled exception.",
                )
            )
        finally:
            # Stop the live context refresher before the final (accurate) emit below.
            if _ctx_task is not None:
                _ctx_task.cancel()
                try:
                    await _ctx_task
                except BaseException:
                    pass
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
                _status = (
                    "complete"
                    if task_succeeded
                    and (not isinstance(final_state, dict) or final_state.get("error") is None)
                    else "error"
                )
                _answer = (
                    final_state.get("final_answer", "") if isinstance(final_state, dict) else ""
                )
                # Emit idle status and stub context before turn.done so the frontend
                # can update the agent indicator before the turn completes
                await events.emit(
                    "agent_status",
                    status={
                        "brain": {
                            "model": os.getenv("BRAIN_MODEL", "gemma-31b"),
                            "endpoint": os.getenv("GEMMA_BASE", "http://localhost:8000/v1"),
                            "state": "idle",
                            "node": "chat_node",
                            "thinkingBudget": 0,
                        },
                        "nervousSystem": {
                            "name": "MCP bridge",
                            "transport": "stdio",
                            "state": "connected",
                            "toolsRegistered": 0,
                        },
                        "hands": {"skills": []},
                    },
                )
                # Final, accurate context fill (peak prompt_tokens high-water mark).
                # Persist it as this session's carried fill so the NEXT turn's strip
                # starts from here instead of 0%.
                _final_peak = call_counter.get_peak_prompt_tokens()
                _log.info("ctx-persist: session=%s final_peak=%d", session_id, _final_peak)
                if _final_peak > 0 and session_id:
                    self._session_ctx_fill[session_id] = _final_peak
                await events.emit("context", window=_context_window(_final_peak))
                await events.emit("turn.done", status=_status, final_answer=_answer)
            except Exception:
                pass
            if _token is not None:
                events.current_emitter.reset(_token)
            if _counter_token is not None:
                call_counter.reset(_counter_token)
            if _manifest_token is not None:
                client_context.reset_manifest(_manifest_token)
            if _ws_root_token is not None:
                client_context.reset_workspace_root(_ws_root_token)
            await self._redis.xack(GOALS_STREAM, GOALS_GROUP, msg_id)

    async def _write_result(self, task_id: str, result: dict) -> None:
        key = f"{RESULT_PREFIX}{task_id}"
        await self._redis.set(key, json.dumps(result, default=str), ex=RESULT_TTL)
        await self._redis.publish(key, "ready")

    async def _ensure_group(self) -> None:
        try:
            await self._redis.xgroup_create(
                GOALS_STREAM,
                GOALS_GROUP,
                id="0",
                mkstream=True,
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
