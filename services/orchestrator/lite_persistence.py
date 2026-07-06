"""Durable suspend/resume for the lite orchestrator over LocalStore checkpoints.

Replaces LangGraph's per-thread checkpointer with an explicit snapshot at each
suspendable boundary. Best-effort: a persistence failure logs and continues (the
goal still runs; only crash-resume is lost), matching the graph path's tolerance.
"""

from __future__ import annotations

import logging

from services.orchestrator.lite_state import restore, snapshot

_log = logging.getLogger("lite_persistence")


async def save_suspend(store, task_id: str, state: dict, phase: str) -> None:
    try:
        await store.checkpoint_put(task_id, {"phase": phase, "state": snapshot(state)})
    except Exception:  # noqa: BLE001 — persistence is best-effort
        _log.warning("lite checkpoint save failed for %s", task_id, exc_info=True)


async def load_resume(store, task_id: str) -> tuple[dict, str] | None:
    try:
        payload = await store.checkpoint_get(task_id)
    except Exception:  # noqa: BLE001
        _log.warning("lite checkpoint load failed for %s", task_id, exc_info=True)
        return None
    if not payload or "state" not in payload:
        return None
    return restore(payload["state"]), payload.get("phase", "assess")


async def clear(store, task_id: str) -> None:
    try:
        await store.checkpoint_delete(task_id)
    except Exception:  # noqa: BLE001
        _log.debug("lite checkpoint clear failed for %s", task_id, exc_info=True)
