"""Single-process local-harness entrypoint: gateway + orchestrator in one loop.

The `OrchestratorProcess` (services/orchestrator/main.py) is already a valid
`runtime` for the gateway — its `bus`/`signals`/`results`/`_goal_queue`/
`submit_goal` are all set up in `__init__`, before `run()` is even called — so
no `run()` split is needed here. This module just constructs the process,
builds the gateway app against it, and drives both on ONE asyncio loop so
they share the in-process EventBus/SignalRegistry/ResultRegistry directly
(no Redis, no second process).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

import uvicorn

from services.orchestrator.main import OrchestratorProcess, _setup_logging
from services.ws_gateway.config import Config
from services.ws_gateway.server import build_app

_log = logging.getLogger("local")


async def serve() -> None:
    _setup_logging()
    proc = OrchestratorProcess()  # bus/signals/results/_goal_queue ready now
    app = build_app(Config.from_env(), runtime=proc)  # gateway shares the proc's in-proc runtime
    host = os.getenv("LOCAL_HOST", "127.0.0.1")
    port = int(os.getenv("LOCAL_PORT", "8787"))
    # loop="none": uvicorn runs on the CURRENT running loop instead of creating
    # its own, so proc.run() and server.serve() share exactly one asyncio loop.
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="info", loop="none")
    )

    loop = asyncio.get_running_loop()

    def _on_signal(sig: int) -> None:
        _log.info("signal %d — shutting down", sig)
        server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
        except NotImplementedError:
            # Signal handlers aren't available on some platforms (e.g. Windows
            # event loops); uvicorn's own signal handling still applies.
            pass

    # proc.run() builds StorageManager/MCP/graph then drains the goal queue; goals the
    # gateway submits before the loop is ready simply wait in the queue.
    orch_task = asyncio.create_task(proc.run(), name="orchestrator")
    try:
        await server.serve()  # blocks until shutdown signal
    finally:
        await proc.stop()  # sets the shutdown event so _loop exits
        orch_task.cancel()
        try:
            await orch_task
        except asyncio.CancelledError:
            pass


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
