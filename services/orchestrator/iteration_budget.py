"""Iteration budget for the ReAct loop — a pure, thread-safe step counter.

Replaces the bare ``for step in range(max_steps)`` hard cut in
``AsyncOrchestrator.react_execute``. The budget:

  * consumes one unit per ReAct turn,
  * grants exactly ONE grace turn after exhaustion (a final chance for the
    model to call ``finish``),
  * refunds the unit for cheap, read-only iterations (see ``CHEAP_TOOLS``)
    so inspection calls do not starve genuine work.

Pure module: no async, no I/O, no orchestrator imports. Fully unit-testable
on its own.
"""

from __future__ import annotations

import threading

# Tool names whose iterations are refunded: pure reads / inspection that
# should not eat into the working budget. Keep this in sync with the
# read-only tools exposed by AsyncOrchestrator.react_execute.
CHEAP_TOOLS: frozenset[str] = frozenset({
    "read_file",
    "list_dir",
    "code_semantic_search",
})


class IterationBudget:
    """Thread-safe iteration counter with a one-shot grace call.

    ``consume()`` decrements the budget by one and returns whether the turn
    was allowed. Once ``used`` reaches ``max_total``, ``consume()`` returns
    ``False`` and ``grace()`` may be called exactly once to allow a single
    final turn. ``refund()`` returns one unit (used for cheap read-only
    turns) but never lets ``used`` go below zero and never raises ``used``
    above ``max_total``.

    Additionally, ``record_turn()`` increments an absolute turn counter that
    cannot be refunded. This hard ceiling ensures that even a stream of
    distinct cheap reads with no progress eventually halts.
    """

    def __init__(self, max_total: int):
        self.max_total = max_total
        self._used = 0
        self._grace_used = False
        self._absolute_turns = 0  # hard ceiling: increments per turn, no refunds
        self._lock = threading.Lock()

    def consume(self) -> bool:
        """Consume one iteration. Returns True if the turn is allowed."""
        with self._lock:
            if self._used >= self.max_total:
                return False
            self._used += 1
            return True

    def refund(self) -> None:
        """Give back one iteration (cheap read-only turns).

        Never drops below zero; never exceeds ``max_total`` (a refund cannot
        manufacture budget that was never consumed).
        """
        with self._lock:
            if self._used > 0:
                self._used -= 1

    def grace(self) -> bool:
        """Grant the single grace turn after exhaustion.

        Returns True the first time it is called, False on every subsequent
        call — so the grace turn fires exactly once.
        """
        with self._lock:
            if self._grace_used:
                return False
            self._grace_used = True
            return True

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.max_total - self._used)

    @property
    def grace_used(self) -> bool:
        with self._lock:
            return self._grace_used

    def record_turn(self) -> bool:
        """Record an absolute turn (refund-independent counter).

        Returns True if the turn count is still within the hard ceiling.
        Once ``absolute_turns`` reaches ``2 * max_total``, returns False.

        This hard limit ensures that even a stream of distinct cheap reads
        (which are refunded and never exhaust the budget) eventually halts.
        The 2x multiplier allows for interleaving of cheap and expensive turns.
        """
        with self._lock:
            if self._absolute_turns >= 2 * self.max_total:
                return False
            self._absolute_turns += 1
            return True

    @property
    def absolute_turns(self) -> int:
        with self._lock:
            return self._absolute_turns


__all__ = ["IterationBudget", "CHEAP_TOOLS"]
