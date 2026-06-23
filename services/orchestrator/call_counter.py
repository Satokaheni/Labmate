"""
Per-task LLM call counter (A/B routing instrumentation).

Counts litellm completions across a single task so the A/B routing harness can
compare ARM A ("multi", decompose + per-sub-intent confidence checks) against
ARM B ("single", whole message = one intent). The two arms differ ONLY in
whether decompose() runs, so the litellm-completion count is the cleanest proxy
for the cost difference.

Design mirrors events.py's ContextVar pattern: a task-scoped mutable counter
lives in the `current_counter` ContextVar, set once per goal in main._handle.
A litellm success callback (registered ONCE at import) increments the current
task's counter on EACH successful completion. The callback is defensive: if no
counter is set in the ContextVar it no-ops, and it never raises.

WHAT IT COUNTS: every successful litellm completion made while the counter is
set in this task's context — select() samples, decompose(), confidence checks,
plan_tool_call() retries, architect()/editor() node calls, ReAct turns, the
critique skill's own Gemma calls if they run in-process, etc. It is therefore an
APPROXIMATE per-task total of model round-trips, not a count of any one node.
Counts made off the task's context (no counter set) are ignored.

CRITICAL: never write to stdout — log to stderr. Never raise from the callback.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any

_log = logging.getLogger("call_counter")


class CallCounter:
    """A simple mutable per-task completion counter."""

    __slots__ = ("count",)

    def __init__(self) -> None:
        self.count = 0

    def increment(self) -> None:
        self.count += 1


# Task-scoped counter. No counter set (e.g. unit tests, off-task calls) => callback no-ops.
current_counter: ContextVar["CallCounter | None"] = ContextVar(
    "current_counter", default=None
)


def start() -> Any:
    """Set a fresh counter in the ContextVar; return the token for reset().

    Mirrors events.current_emitter.set(...): call at the start of handling a task
    and pass the returned token to reset() in a finally block.
    """
    return current_counter.set(CallCounter())


def reset(token: Any) -> None:
    """Restore the previous ContextVar state (pair with start())."""
    if token is not None:
        current_counter.reset(token)


def get_count() -> int:
    """Current task's completion count, or 0 when no counter is set."""
    counter = current_counter.get()
    return counter.count if counter is not None else 0


def _increment_current() -> None:
    """Increment the current task's counter; no-op when none is set. Never raises."""
    try:
        counter = current_counter.get()
        if counter is not None:
            counter.increment()
    except Exception:  # pragma: no cover - defensive: a counter bug must never break a task
        pass


# ── litellm success-callback registration (once, at import) ──────────────────

try:
    import litellm
    from litellm.integrations.custom_logger import CustomLogger

    class _CallCountLogger(CustomLogger):
        """Increments the current task's counter on each successful completion."""

        def log_success_event(self, kwargs, response_obj, start_time, end_time):  # noqa: D401
            _increment_current()

        async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):  # noqa: D401
            _increment_current()

    _CALL_COUNT_LOGGER = _CallCountLogger()

    # Append (don't replace) so we never clobber other registered callbacks.
    if not any(isinstance(cb, _CallCountLogger) for cb in litellm.callbacks):
        litellm.callbacks.append(_CALL_COUNT_LOGGER)
except Exception:  # pragma: no cover - litellm import/registration must never break startup
    _log.warning("call_counter: failed to register litellm callback", exc_info=True)
    _CALL_COUNT_LOGGER = None
