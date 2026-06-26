# services/orchestrator/loop_detection.py
"""
Pure, dependency-free tool-loop / no-progress detector for the ReAct executor.

A weak local Q4 model often repeats the same tool call with the same arguments,
or cycles a small set of calls, and burns every step without making progress.
This module turns each (tool_name, normalized_arguments) pair into a
deterministic signature and reports when the executor should break early.

Design rules (see the implementation plan's Global Constraints):
  - No LLM, no async, no Redis, no imports from the orchestrator. Pure stdlib.
  - Deterministic normalization: json.dumps(args, sort_keys=True, default=str).
  - Conservative: trips only on an immediate consecutive repeat OR a short cycle
    with no new signature; legitimately distinct calls never trip it.
  - stderr logging only (never stdout — MCP rule).
"""
from __future__ import annotations

import json
import logging
import os

_log = logging.getLogger("loop_detection")

# Number of consecutive identical signatures that counts as a stuck loop.
# Matches the env-knob style used in graph.py (module-level getenv + cast).
LOOP_REPEAT_LIMIT = int(os.getenv("LOOP_REPEAT_LIMIT", "2"))


def call_signature(name: str, args: dict) -> str:
    """Deterministic signature for a tool call.

    Argument key order must never change the signature, so we sort keys. Values
    that are not JSON-serializable fall back to str() rather than raising — the
    detector must never crash the executor.
    """
    try:
        norm = json.dumps(args or {}, sort_keys=True, default=str)
    except Exception:
        norm = str(args)
    return f"{name}::{norm}"


class LoopDetector:
    """Ingests call signatures and reports whether the loop should break.

    `record(sig)` appends a signature and returns the current should_break().
    `should_break()` is True when EITHER:
      - the last `repeat_limit` signatures are all identical (a repeat), OR
      - within the recent `cycle_window` no signature has been seen for the
        FIRST time in the last `repeat_limit` steps AND the window cycles a
        small set (<= repeat_limit distinct signatures) — a no-progress cycle.
    """

    def __init__(self, repeat_limit: int | None = None, cycle_window: int = 6) -> None:
        self.repeat_limit = LOOP_REPEAT_LIMIT if repeat_limit is None else repeat_limit
        self.cycle_window = cycle_window
        self._sigs: list[str] = []
        self._reason = ""

    def reset(self) -> None:
        """Clear history — call at the per-goal boundary."""
        self._sigs = []
        self._reason = ""

    def record(self, signature: str) -> bool:
        self._sigs.append(signature)
        return self.should_break()

    def reason(self) -> str:
        return self._reason

    def should_break(self) -> bool:
        if self.repeat_limit < 1:
            return False
        sigs = self._sigs
        n = len(sigs)
        if n < self.repeat_limit:
            return False

        # 1) Immediate consecutive repeat.
        tail = sigs[-self.repeat_limit:]
        if len(set(tail)) == 1:
            self._reason = "repeat"
            return True

        # 2) No-progress cycle. Look at the recent window; if it cycles a small
        # set of signatures and nothing in the last `repeat_limit` steps is a
        # FIRST-time signature (i.e. no genuine progress), treat it as a loop.
        window = sigs[-self.cycle_window:]
        if len(window) >= 2 * self.repeat_limit:
            distinct = set(window)
            # Small alternating/cycling set: distinct count is at most repeat_limit
            # and every recent step was already seen earlier in the window.
            if len(distinct) <= self.repeat_limit:
                recent = sigs[-self.repeat_limit:]
                earlier = set(sigs[:-self.repeat_limit])
                if all(s in earlier for s in recent):
                    self._reason = "cycle"
                    return True

        return False
