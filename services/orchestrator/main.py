"""
Orchestrator process entrypoint.

Bootstraps in order:
  1. Logging (stderr only)
  2. StorageManager  (MongoDB + Redis — reads MONGO_URI / REDIS_URL)
  3. MCPClientManager (spawns MCP bridge subprocess, waits for ready)
  4. LangGraph       (build_graph with local SqliteSaver checkpointer)
  5. Goal loop       (XREADGROUP labmate:goals → run_task → write result)

Env vars:
  MONGO_URI          mongodb://localhost:27017/labmate
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

from services.orchestrator import call_counter, client_context, ctx_window, events, skill_curator
from services.orchestrator.coding_orchestrator import AsyncOrchestrator, CodingOrchestrator
from services.orchestrator.completion_guard import reconcile_final_answer
from services.orchestrator.graph import GEMMA_BASE, QWEN_BASE, build_graph
from services.orchestrator.inproc_bus import EventBus, ResultRegistry, SignalRegistry
from services.orchestrator.mcp_client_manager import MCPClientManager
from services.orchestrator.session_search import SessionSearch
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

# Per-request context window the model actually serves (= llama-server --ctx-size
# DIVIDED BY --parallel). This ceiling drives the context gauge (used/free shown to
# the frontend) and the auto-compact thresholds, so it must equal the PER-SLOT window.
#
# These are the STARTUP DEFAULTS only. run() overwrites them by querying llama-server's
# /props for the actual loaded window (see ctx_window.resolve_ctx_window), so the gauge
# tracks the served model automatically without hand-syncing CTX_WINDOW on a model swap.
# The CTX_WINDOW env is the fallback when that query fails; 131072 is the last resort.
CTX_TOKENS = int(os.getenv("CTX_WINDOW", "131072"))
FULL_THRESH = int(CTX_TOKENS * 0.85)

# How often, during a running turn, to re-emit the context-window telemetry so the
# frontend strip climbs live instead of only updating once at turn end. 0 disables
# the live refresher (a single emit at turn start + one at turn end still fire).
CONTEXT_REFRESH_S = float(os.getenv("CONTEXT_REFRESH_SECONDS", "2.0"))


def _context_window(used_fallback: int = 0, used_floor: int = 0) -> dict:
    """Build the context-window telemetry payload from the live token high-water mark.

    `used` is the peak prompt_tokens seen across THIS task's LLM calls. The per-task
    counter resets each turn, so before this turn's first model call we fall back to
    `used_fallback` — the carried fill from the prior turn in this session — so the
    strip doesn't drop to 0% at the start of every new message. Once a real peak is
    recorded it wins over the fallback (so the value still reflects compaction, which
    shrinks the prompt).

    `used_floor` is a *known-real* minimum: the actual current context size measured
    with the Gemma tokenizer via build_context() at turn start. It is a HARD floor —
    the reported fill never drops below it — because it reflects the accumulated
    conversation history that genuinely occupies the window. This is what keeps the
    strip from resetting to 0% on message 2+ even when no completion reports
    usage.prompt_tokens (e.g. the streaming final-answer path, or an endpoint that
    omits usage), which would otherwise leave both the peak and the carried fill at 0.

    The whole measured fill is attributed to `conversation` — the dominant real
    component — since usage data carries no per-segment breakdown.
    """
    ctx_max = CTX_TOKENS
    _peak = call_counter.get_peak_prompt_tokens()
    _base = _peak if _peak > 0 else max(used_fallback, 0)
    ctx_used = min(max(_base, max(used_floor, 0)), ctx_max)
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
    used_floor: int = 0,
) -> None:
    """Re-emit the context window every `interval_s` until cancelled at turn end.

    Created with asyncio.create_task AFTER the per-task counter contextvar is set,
    so it inherits that context and reports the live peak. `used_fallback` carries
    the prior turn's fill so the strip shows it until this turn's first model call;
    `used_floor` is the real measured context size (a hard minimum) so the strip
    never sits below the actual fill even before the first completion reports usage.
    Best-effort: a failed emit is swallowed; cancellation ends the loop cleanly.
    """
    try:
        while True:
            await asyncio.sleep(interval_s)
            try:
                await emitter.emit(
                    "context",
                    window=_context_window(used_fallback, used_floor),
                )
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


# P2-D: the pod CodeGraph embedder exists ONLY as the no-client / headless fallback.
# When a capable client hosts CodeGraph (as an MCP server via ~/.labmate/mcp.json), the
# pod `code_semantic_search` is already excluded for that client by build_tool_list (it
# advertises the pod tool only when the manifest declares it). Set ENABLE_POD_CODEGRAPH=0
# in a client-first deployment to skip spawning the embedder entirely (saves the index +
# watch cost); no-client tasks then have no semantic search. Default 1 = unchanged.
def pod_codegraph_enabled() -> bool:
    """Whether the orchestrator should spawn the pod CodeGraph embedder at startup.

    Read once at process start (run()); set the env before launching the orchestrator.
    """
    return os.getenv("ENABLE_POD_CODEGRAPH", "1").lower() not in ("0", "false", "no")


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
        # In-process event/signal/result primitives (Piece 4): the agent event
        # stream and steer/cancel signals route through these instead of Redis.
        # Goals/results transport and skill/tool dispatch still use self._redis
        # until later tasks (T5/T6) remove it.
        self.bus = EventBus()
        self.signals = SignalRegistry()
        self.results = ResultRegistry()
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

            # Single source of truth for the context gauge: ask llama-server what
            # context window it actually loaded (best-effort; falls back to the
            # CTX_WINDOW env, then 131072, if /props is unreachable). Updating the
            # module globals is safe because _context_window()/FULL_THRESH read them
            # at call time, and task processing (which emits `context` events) begins
            # only after this point.
            global CTX_TOKENS, FULL_THRESH
            CTX_TOKENS = await ctx_window.resolve_ctx_window(GEMMA_BASE, fallback=CTX_TOKENS)
            FULL_THRESH = int(CTX_TOKENS * 0.85)
            _log.info("context window: %d tokens (full-threshold %d)", CTX_TOKENS, FULL_THRESH)

            self._mcp = MCPClientManager(_build_mcp_params())
            await self._mcp.start()
            try:
                await self._mcp.wait_ready(timeout=30.0)
                _log.info("MCP bridge ready (%d tools)", len(self._mcp.tools))
            except TimeoutError:
                _log.warning("MCP bridge did not become ready within 30 s — continuing")

            if pod_codegraph_enabled():
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
            else:
                # P2-D: client-first deployment — clients host CodeGraph themselves.
                self._codegraph_mcp = None
                _log.info("pod codegraph embedder disabled (ENABLE_POD_CODEGRAPH=0)")

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
            # Piece 4: steer/cancel now read from the in-process SignalRegistry
            # instead of Redis; wired alongside redis so the ReAct loop's
            # `self.signals is not None` guard degrades cleanly in tests that
            # never set it.
            async_orch.signals = self.signals
            async_orch.codegraph_mcp = self._codegraph_mcp
            async_orch.session_search = SessionSearch(_sm)
            # Wire context_manager for conversation continuity (best-effort)
            try:
                async_orch.context_manager = _sm.context_manager
            except Exception:
                pass  # context_manager may be absent in test/offline environments

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
            graph, _cp = build_graph(orch=orch, async_orch=async_orch)
            orch.graph = graph
            # Wire context_manager to CodingOrchestrator (best-effort)
            try:
                orch.context_manager = _sm.context_manager
            except Exception:
                pass  # context_manager may be absent in test/offline environments

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

                session_ids = await storage._db["chat_turns"].distinct("sessionId")
                for session_id in session_ids[:BG_COMPACT_MAX_SESSIONS]:
                    if self._shutdown.is_set():
                        break
                    if not session_id:
                        continue
                    result = await storage.context_manager.maybe_background_compact(
                        session_id,
                        _bg_llm,
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
        # Known-real context fill measured at turn start (Gemma-tokenized). Pre-declared
        # so the turn-end finally block can always reference it as the strip's hard floor.
        _ctx_floor = 0

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
                await self._write_result(task_id, {"ok": True, **result})
                return

            _emitter = events.EventEmitter(self.bus, task_id)
            _token = events.current_emitter.set(_emitter)
            # Per-task LLM call counter (A/B instrumentation): set a fresh counter for
            # this task's context; the litellm success callback increments it.
            _counter_token = call_counter.start()
            # Per-task client capability manifest: parse from payload and set in context.
            # When no manifest is present, behavior is unchanged (full tool list).
            _manifest_token = client_context.set_manifest(
                parse_manifest(payload.get("client_capabilities"))
            )
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

            # Load AGENT.md (or workspace.instructions DB field) — pinned for this session.
            # Loaded here (before the context strip's turn-start emit) because it feeds the
            # build_context() measurement used as the strip's known-real fill FLOOR below.
            agent_instructions = ""
            try:
                agent_instructions = await storage.workspaces.load_agent_instructions(workspace_id)
            except Exception:
                pass

            # Measure the REAL current context fill (Gemma-tokenized) for this session
            # BEFORE running the graph. This same build_context result gates auto-compact
            # (below) and doubles as the context-strip FLOOR: the accumulated conversation
            # history genuinely occupies the window, so the strip must never show less than
            # this — not even on message 2+ when no completion reports usage.prompt_tokens
            # (streaming final-answer path / usage-less endpoints) and the carried peak is 0.
            ctx_check = None
            try:
                ctx_check = await storage.context_manager.build_context(
                    session_id=session_id,
                    current_task=task_text,
                    system_prompt="",
                    agent_instructions=agent_instructions,
                )
                _ctx_floor = max(int(ctx_check.total_tokens or 0), 0)
            except Exception as exc:
                _log.warning("ctx-floor build_context failed (non-fatal): %s", exc)

            # Populate the strip immediately. Show the larger of the fill CARRIED from the
            # prior turn and the measured floor, so it reflects the accumulated context
            # (never resetting to 0%) — then refresh live as this turn's real peak is measured.
            _carried_fill = self._session_ctx_fill.get(session_id, 0)
            _log.info(
                "ctx-carry: session=%s carried=%d floor=%d", session_id, _carried_fill, _ctx_floor
            )
            # Persist the measured floor as this session's carry right away, so even if this
            # turn's peak counter never populates (no usage reported), the NEXT turn still
            # starts from a real, non-zero fill instead of 0.
            if _ctx_floor > 0 and session_id:
                self._session_ctx_fill[session_id] = max(
                    self._session_ctx_fill.get(session_id, 0), _ctx_floor
                )
            await _emitter.emit("context", window=_context_window(_carried_fill, _ctx_floor))
            if CONTEXT_REFRESH_S > 0:
                _ctx_task = asyncio.create_task(
                    _context_refresh_loop(
                        _emitter, used_fallback=_carried_fill, used_floor=_ctx_floor
                    )
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

            # Auto-compact: reuse the context measurement taken above for the strip floor.
            try:
                if ctx_check is not None and ctx_check.total_tokens >= FULL_THRESH:
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
            except Exception as exc:
                _log.warning("auto-compact check failed (non-fatal): %s", exc)

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
            _answer_for_turns = (
                final_state.get("final_answer", "") if isinstance(final_state, dict) else ""
            )
            await self._persist_turns(storage, session_id, task_text, _answer_for_turns)
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
                # starts from here instead of 0%. The measured turn-start floor
                # (_ctx_floor) is the hard minimum: never persist or emit below it, and
                # never let a small/zero peak shrink a carry we already seeded from the
                # floor — otherwise the strip would still reset on the next message when
                # this turn reported no usage.
                _final_peak = call_counter.get_peak_prompt_tokens()
                _log.info(
                    "ctx-persist: session=%s final_peak=%d floor=%d",
                    session_id,
                    _final_peak,
                    _ctx_floor,
                )
                if session_id:
                    _carry = max(_final_peak, _ctx_floor, self._session_ctx_fill.get(session_id, 0))
                    if _carry > 0:
                        self._session_ctx_fill[session_id] = _carry
                await events.emit("context", window=_context_window(_final_peak, _ctx_floor))
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

    async def _persist_turns(
        self, storage: StorageManager, session_id: str, user_text: str, assistant_text: str
    ) -> None:
        """Best-effort: persist the user + assistant turns for this goal to the local
        store so the next turn of this session has conversation continuity. Never
        raises into the caller (a persistence failure must not fail the task)."""
        if not session_id:
            return
        try:
            store = storage.local_store
            if user_text:
                await store.append_turn(session_id, "user", user_text)
            if assistant_text:
                await store.append_turn(session_id, "assistant", assistant_text)
        except Exception:  # noqa: BLE001 — persistence is best-effort
            _log.warning("turn persistence failed for session %s", session_id, exc_info=True)

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
