"""No-progress (idle) breaker for the ReAct loop — pure and thread-safe.

A second guard layered on top of ``IterationBudget``'s step counting. Modeled
on openclaw's ``stepIdleTimeoutBreaker``: a PURE counter that

  * increments on a turn that made NO completed progress,
  * RESETS to 0 the moment a turn makes real progress (new assistant content,
    a tool call/result, or ``finish``),
  * trips (hard stop) once the consecutive no-progress count reaches ``cap``.

Decision table (per turn):
  * made_progress is False, cap > 0  -> consecutive += 1; tripped when >= cap
  * made_progress is True            -> consecutive = 0;  tripped is False
  * cap == 0                         -> never trips (counter still climbs)

Pure module: no async, no I/O, no orchestrator imports. Fully unit-testable.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class ProgressStep:
    """Result of a single ``ProgressBreaker.step`` call."""

    consecutive: int
    tripped: bool


class ProgressBreaker:
    """Thread-safe consecutive-no-progress counter with a trip cap.

    ``step(made_progress, *, cap=None)`` records one turn and returns a
    :class:`ProgressStep`. A turn that made no completed progress increments
    the consecutive counter; a turn that made progress resets it to 0. The
    breaker trips when the consecutive count reaches ``cap`` (``cap > 0``); a
    ``cap`` of ``0`` disables tripping entirely. When ``cap`` is ``None`` the
    ``default_cap`` supplied at construction is used.
    """

    def __init__(self, default_cap: int = 5):
        self.default_cap = default_cap
        self._consecutive = 0
        self._tripped = False
        self._lock = threading.Lock()

    def step(self, made_progress: bool, *, cap: int | None = None) -> ProgressStep:
        effective_cap = self.default_cap if cap is None else cap
        with self._lock:
            if made_progress:
                self._consecutive = 0
            else:
                self._consecutive += 1
            self._tripped = effective_cap > 0 and self._consecutive >= effective_cap
            return ProgressStep(consecutive=self._consecutive, tripped=self._tripped)

    @property
    def consecutive(self) -> int:
        with self._lock:
            return self._consecutive

    @property
    def tripped(self) -> bool:
        with self._lock:
            return self._tripped


__all__ = ["ProgressBreaker", "ProgressStep"]
