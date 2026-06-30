"""
Per-task client capability manifest (context variable).

Mirrors the shape of call_counter.py: a task-scoped ClientManifest lives in the
current_manifest ContextVar, set once per goal in main._handle via parse_manifest().
When no manifest is present (no client attached), behavior is unchanged — the full
tool list is used in PromptAssembler.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from services.orchestrator.tool_manifest import ClientManifest

# Task-scoped manifest. No manifest set (e.g. CLI, off-task calls, legacy) => None.
current_manifest: ContextVar[ClientManifest | None] = ContextVar("current_manifest", default=None)


def set_manifest(manifest: ClientManifest | None) -> Any:
    """Set the client capability manifest for this task; return a token for reset().

    Mirrors call_counter.start(): call at the start of handling a task
    and pass the returned token to reset_manifest() in a finally block.
    """
    return current_manifest.set(manifest)


def reset_manifest(token: Any) -> None:
    """Restore the previous ContextVar state (pair with set_manifest())."""
    if token is not None:
        try:
            current_manifest.reset(token)
        except Exception:  # pragma: no cover - defensive: a context bug must never break a task
            pass


def get_manifest() -> ClientManifest | None:
    """Get the current task's client capability manifest, or None when none is set."""
    return current_manifest.get()
