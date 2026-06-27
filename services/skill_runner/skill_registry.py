from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

import jsonschema
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# CRITICAL: log handler must write to sys.stderr, NEVER sys.stdout.
# The host's stdout is the JSON-RPC channel for any parent MCP server.
log = logging.getLogger("skill_registry")


def _log_task_exception(task: asyncio.Task) -> None:
    """Log exceptions from background tasks; silently ignore cancellations."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("background restart task failed: %r", exc)


# Sentinel object to signal skill process to restart.
_RESTART_SENTINEL = object()


def _drain_inbox(sp: SkillProcess, exc: BaseException) -> None:
    """Fail every pending request in the inbox so callers don't hang.

    When a skill process crashes or is shut down, any pending _SkillReq
    futures must be resolved with an exception, otherwise callers blocked
    on await fut will hang forever.
    """
    while not sp._inbox.empty():
        try:
            req = sp._inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if isinstance(req, _SkillReq) and not req.future.done():
            req.future.set_exception(exc)


@dataclass
class _SkillReq:
    """A tool call request posted to a skill's inbox queue."""
    tool: str
    arguments: dict
    future: asyncio.Future


class SkillUnavailable(Exception):
    pass


class SkillDraining(Exception):
    pass


@dataclass
class SkillManifest:
    name: str            # namespace prefix, e.g. "ast.repo-map"
    command: str         # "node" | "python" | absolute path to a rust binary
    args: list[str]
    env: dict | None = None
    version: str | None = None
    language: str | None = None


@dataclass
class SkillProcess:
    manifest: SkillManifest
    session: ClientSession | None = None
    stack: AsyncExitStack | None = None
    tools: dict[str, dict] = field(default_factory=dict)  # tool_name -> inputSchema
    state: str = "INIT"   # INIT | READY | DRAINING | DEAD
    inflight: int = 0
    restarts: int = 0
    _inbox: asyncio.Queue = field(default_factory=asyncio.Queue)
    _ready: asyncio.Event = field(default_factory=asyncio.Event)
    _run_task: asyncio.Task | None = None


class SkillRegistry:
    """Manages long-lived MCP skill subprocesses.

    CRITICAL: ALL logging must go to sys.stderr. This class may itself
    run as an MCP server (parent stdio channel); stdout must be reserved
    for JSON-RPC framing exclusively.

    anyio cancel-scope rule: each ClientSession is entered AND exited by
    the SAME task. Each SkillProcess owns a dedicated background task
    (_run_skill) that holds the stdio_client and ClientSession for the
    full lifetime. All restarts happen inside that task; cancel scopes
    are NEVER crossed.
    """

    def __init__(self, call_timeout: float = 30.0) -> None:
        self._skills: dict[str, SkillProcess] = {}
        self._tool_index: dict[str, str] = {}   # "ns.tool" -> skill name
        self._call_timeout = call_timeout
        self._lock = asyncio.Lock()

    async def register(self, m: SkillManifest) -> None:
        sp = SkillProcess(manifest=m)
        await self._spawn(sp)
        self._skills[m.name] = sp
        log.info("registered skill: %s (%d tools)", m.name, len(sp.tools))

    async def _spawn(self, sp: SkillProcess) -> None:
        """Start the owning background task and wait for it to signal ready."""
        sp._ready.clear()
        sp._run_task = asyncio.create_task(
            self._run_skill(sp), name=f"skill-{sp.manifest.name}"
        )
        try:
            await asyncio.wait_for(sp._ready.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            sp._run_task.cancel()
            raise RuntimeError(f"Skill {sp.manifest.name!r} did not become ready in 30s")

    async def _run_skill(self, sp: SkillProcess) -> None:
        """Owns stdio_client + ClientSession for the full lifetime of this skill.

        Runs restart loops internally so cancel scopes are NEVER crossed.
        """
        m = sp.manifest
        params = StdioServerParameters(command=m.command, args=m.args, env=m.env)

        while True:  # restart loop
            try:
                async with AsyncExitStack() as stack:
                    read, write = await stack.enter_async_context(stdio_client(params))
                    session = await stack.enter_async_context(ClientSession(read, write))
                    await session.initialize()
                    listed = await session.list_tools()

                    sp.session = session
                    sp.tools = {}
                    sp.state = "READY"
                    for t in listed.tools:
                        qualified = f"{m.name}.{t.name}"
                        sp.tools[t.name] = t.inputSchema
                        self._tool_index[qualified] = m.name

                    sp._ready.set()  # signal callers waiting in _spawn
                    sp.restarts = 0  # reset consecutive failure counter on successful session

                    # Process inbox until cancelled or error
                    while True:
                        req = await sp._inbox.get()
                        if req is _RESTART_SENTINEL:
                            break  # exit the inner while, triggering restart
                        # req is a _SkillReq(tool, arguments, future)
                        try:
                            result = await asyncio.wait_for(
                                session.call_tool(req.tool, req.arguments),
                                timeout=self._call_timeout,
                            )
                            if not req.future.done():
                                req.future.set_result(result)
                        except Exception as exc:
                            if not req.future.done():
                                req.future.set_exception(exc)
            except asyncio.CancelledError as exc:
                async with self._lock:
                    sp.state = "DEAD"
                    sp._ready.clear()
                    _drain_inbox(sp, exc)
                return  # clean shutdown
            except Exception as exc:
                log.error("skill %s crashed: %r — restarting in %ds",
                          m.name, exc, min(2 ** sp.restarts, 30))
                sp.session = None
                async with self._lock:
                    sp.state = "DEAD"
                    sp._ready.clear()
                    _drain_inbox(sp, exc)

            # Backoff before restart
            backoff = min(2 ** sp.restarts, 30)
            sp.restarts += 1
            await asyncio.sleep(backoff)
            sp.state = "INIT"

    async def call_tool(self, qualified_name: str, arguments: dict) -> object:
        ns, _, tool = qualified_name.partition(".")
        sp = self._skills.get(ns)
        if sp is None or sp.state == "DEAD":
            raise SkillUnavailable(qualified_name)
        if sp.state == "DRAINING":
            raise SkillDraining(qualified_name)
        schema = sp.tools.get(tool)
        if schema is None:
            valid = ", ".join(sorted(sp.tools)) or "(none advertised)"
            raise SkillUnavailable(
                f"no tool {tool!r} in skill {ns!r}; valid tools: {valid}"
            )
        # jsonschema gate: validates BEFORE any subprocess round-trip.
        jsonschema.validate(instance=arguments, schema=schema)

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        sp.inflight += 1
        enqueued = False
        try:
            # State-check and put must be atomic under self._lock to prevent
            # a race where _run_skill sets DEAD and drains between check and put.
            async with self._lock:
                if sp.state != "READY":
                    raise SkillUnavailable(qualified_name)
                await sp._inbox.put(_SkillReq(tool=tool, arguments=arguments, future=fut))
                enqueued = True
            # Await the future OUTSIDE the lock so _run_skill can process concurrently
            return await fut
        except SkillUnavailable:
            sp.inflight -= 1
            raise
        except Exception as exc:
            log.error("call %s failed: %r", qualified_name, exc)   # -> stderr only
            task = asyncio.create_task(self._maybe_restart(sp))
            task.add_done_callback(_log_task_exception)
            raise
        finally:
            if enqueued:
                sp.inflight -= 1

    async def _maybe_restart(self, sp: SkillProcess) -> None:
        """Signal the owning task to restart by sending the sentinel.

        The owning task (_run_skill) handles close/reopen internally,
        so cancel scopes are never crossed.
        """
        async with self._lock:
            if sp.state == "DEAD":
                return
            sp.state = "DEAD"
            # Remove this skill's tools from the routing index.
            dead_keys = [k for k, v in self._tool_index.items()
                         if v == sp.manifest.name]
            for k in dead_keys:
                self._tool_index.pop(k, None)
        await sp._inbox.put(_RESTART_SENTINEL)

    async def reload(self, name: str) -> None:
        """Hot-reload: drain in-flight calls, then signal restart."""
        sp = self._skills[name]
        async with self._lock:
            sp.state = "DRAINING"           # router stops accepting new calls
        while sp.inflight > 0:
            await asyncio.sleep(0.05)       # let in-flight calls finish
        sp._ready.clear()                   # clear BEFORE sending sentinel
        await sp._inbox.put(_RESTART_SENTINEL)
        await sp._ready.wait()              # now actually waits for the new session

    async def supervise(self, interval: float = 5.0) -> None:
        """Background health loop. Run as an asyncio.Task at harness startup."""
        while True:
            await asyncio.sleep(interval)
            for sp in list(self._skills.values()):
                if sp.state == "READY" and not _process_alive(sp):
                    log.warning("skill %s died unexpectedly", sp.manifest.name)
                    await self._maybe_restart(sp)


def _process_alive(sp: SkillProcess) -> bool:
    """Check if the subprocess underlying the MCP session is still running.

    The exact handle depends on mcp SDK internals; this conservative check
    treats a process as alive while it holds a session and is not DEAD/INIT.
    The supervise() loop overrides this in tests via monkeypatch.
    """
    return sp.session is not None and sp.state not in ("DEAD", "INIT")
