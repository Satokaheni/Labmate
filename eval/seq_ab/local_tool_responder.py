"""Local-tool responder for the A/B harness.

NOTE — eval-tooling only, describes the harness's OWN simulated transport, not
the current live architecture: since the local-state-sqlite rearchitecture, the
live stack runs `services.local.main` as one co-located process (gateway +
orchestrator, one asyncio loop, SQLite LocalStore) and client-delegated tools
(read_file/write_file/list_dir) dispatch directly in-process via
`execute_local_tool` — there is no Redis event round-trip on the live path
anymore. This A/B harness (`eval/seq_ab/run_seq_ab.py`, RunPod-only) still
drives goals through a standalone Redis stream to simulate a detached client:
it emits a `tool.request` on labmate:events:<task_id> and BLOCKS on
labmate:tool-results:<task_id> for a matching frame, so those tools would
otherwise time out (30s) with no client attached. This responder answers them:
it tails the event stream, runs each tool with the CLI's execute_local_tool,
and writes the result frame the orchestrator awaits.

RunPod-only (sync redis) — kept as-is pending the eval-tooling follow-up that
retires the Redis-based A/B harness in favor of the SQLite/in-process model.
The pure handle_event_frame() is unit-tested; the thread loop is validated by
the live A/B.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from services.cli.local_tool_executor import execute_local_tool

EVENTS_PREFIX = "labmate:events:"
RESULTS_PREFIX = "labmate:tool-results:"
RESULTS_MAXLEN = 200


def events_stream_key(task_id: str) -> str:
    return f"{EVENTS_PREFIX}{task_id}"


def result_stream_key(task_id: str) -> str:
    return f"{RESULTS_PREFIX}{task_id}"


def handle_event_frame(frame: dict, workspace: str) -> dict | None:
    """Pure: a decoded event frame -> the result-frame payload, or None.

    Matches local_tools.write_tool_result's payload shape so the orchestrator's
    request_local_tool can match it: {"tool_request_id", "result", "error"}.
    """
    if not isinstance(frame, dict) or frame.get("type") != "tool.request":
        return None
    tool_request_id = frame.get("tool_request_id", "")
    name = frame.get("name", "")
    args = frame.get("args", {}) or {}
    try:
        result: Any = execute_local_tool(name, args, workspace=workspace)
        return {"tool_request_id": tool_request_id, "result": result, "error": None}
    except Exception as exc:  # noqa: BLE001 — surface any fs/path error to the model
        return {"tool_request_id": tool_request_id, "result": None, "error": str(exc)}


class LocalToolResponder:
    """Background thread: drain tool.request events for one task, answer them."""

    def __init__(self, redis_client, task_id: str, workspace: str):
        self._r = redis_client
        self._task_id = task_id
        self._workspace = workspace
        self._events_key = events_stream_key(task_id)
        self._results_key = result_stream_key(task_id)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_id = "0-0"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _emit_result(self, payload: dict) -> None:
        self._r.xadd(
            self._results_key,
            {"result": json.dumps(payload, default=str)},
            maxlen=RESULTS_MAXLEN,
            approximate=True,
        )

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                resp = self._r.xread({self._events_key: self._last_id}, count=50, block=500)
            except Exception:
                time.sleep(0.1)
                continue
            if not resp:
                continue
            for _stream, entries in resp:
                for entry_id, fields in entries:
                    self._last_id = entry_id
                    raw = fields.get("event") or fields.get(b"event")
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    if not raw:
                        continue
                    try:
                        frame = json.loads(raw)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    payload = handle_event_frame(frame, self._workspace)
                    if payload is not None:
                        self._emit_result(payload)
