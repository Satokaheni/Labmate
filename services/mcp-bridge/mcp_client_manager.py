from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters, stdio_client


class CircuitOpenError(Exception):
    """Raised when the circuit breaker trips after repeated server crashes."""


@dataclass
class _Req:
    name:    str
    args:    dict[str, Any]
    future:  asyncio.Future
    timeout: float = 30.0


class MCPClientManager:
    """
    Single owning task for the MCP session lifecycle.

    CRITICAL INVARIANT: stdio_client() and ClientSession() are entered AND
    exited inside _run(), in one dedicated asyncio.Task. Callers never hold
    session references — they submit via _inbox and await futures.
    """

    def __init__(
        self,
        params:       StdioServerParameters,
        *,
        max_failures: int   = 5,
        window:       float = 60.0,
        call_timeout: float = 30.0,
    ) -> None:
        self._params       = params
        self._inbox:       asyncio.Queue[_Req] = asyncio.Queue()
        self._ready        = asyncio.Event()
        self._task:        asyncio.Task | None = None
        self._failures:    deque[float] = deque()
        self._max_failures = max_failures
        self._window       = window
        self._call_timeout = call_timeout
        self.tools:        list = []

    async def start(self) -> None:
        """Create the owning lifecycle task. Call once before any tool calls."""
        self._task = asyncio.create_task(self._run(), name='mcp-lifecycle')

    async def wait_ready(self, timeout: float = 10.0) -> None:
        """Block until the session is initialized."""
        await asyncio.wait_for(self._ready.wait(), timeout=timeout)

    async def call_tool(
        self,
        name:    str,
        args:    dict[str, Any],
        timeout: float | None = None,
    ) -> Any:
        """Submit a tool call. Many coroutines may call this concurrently."""
        fut = asyncio.get_running_loop().create_future()
        await self._inbox.put(_Req(name, args, fut, timeout or self._call_timeout))
        return await fut

    async def shutdown(self) -> None:
        """Cancel the owning task and wait for it to exit."""
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def _breaker_open(self) -> bool:
        now = time.monotonic()
        while self._failures and now - self._failures[0] > self._window:
            self._failures.popleft()
        return len(self._failures) >= self._max_failures

    def _drain_with(self, exc: Exception) -> None:
        while not self._inbox.empty():
            try:
                req = self._inbox.get_nowait()
                if not req.future.done():
                    req.future.set_exception(exc)
            except asyncio.QueueEmpty:
                break

    async def _run(self) -> None:
        """
        The single owning task. Both stdio_client() and ClientSession() enter
        and exit here — in the SAME task — satisfying anyio's cancel-scope rule.
        """
        backoff = 0.5
        while True:
            if self._breaker_open():
                err = CircuitOpenError(
                    f'MCP server crashed {self._max_failures}+ times '
                    f'in {self._window}s; circuit open'
                )
                self._drain_with(err)
                await asyncio.sleep(self._window)
                self._failures.clear()
                continue

            try:
                async with stdio_client(self._params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result     = await session.list_tools()
                        self.tools = result.tools
                        self._ready.set()
                        backoff = 0.5
                        await self._serve(session)

            except asyncio.CancelledError:
                return
            except Exception:
                self._failures.append(time.monotonic())
                self._ready.clear()
                jitter = random.uniform(0, backoff)
                await asyncio.sleep(backoff + jitter)
                backoff = min(backoff * 2, 30.0)

    async def _serve(self, session: ClientSession) -> None:
        """Multiplex tool calls from the inbox onto the session."""
        while True:
            req = await self._inbox.get()
            try:
                with anyio.fail_after(req.timeout):
                    result = await session.call_tool(req.name, req.args)
                if not req.future.done():
                    req.future.set_result(result)
            except TimeoutError as exc:
                if not req.future.done():
                    req.future.set_exception(exc)
            except Exception as exc:
                if not req.future.done():
                    req.future.set_exception(exc)
                raise
