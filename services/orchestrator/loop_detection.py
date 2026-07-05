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

import hashlib
import json
import logging
import os

_log = logging.getLogger("loop_detection")

# Number of consecutive identical signatures that counts as a stuck loop.
# Matches the env-knob style used in graph.py (module-level getenv + cast).
LOOP_REPEAT_LIMIT = int(os.getenv("LOOP_REPEAT_LIMIT", "2"))

# Mutating tools edit state (files / sandbox writes). A weak model legitimately
# retries "edit, run tests, see failure, edit again", so identical consecutive
# mutating calls must be tolerated longer than a read/inspect repeat before the
# detector halts. Cycle detection is unaffected.
MUTATING_TOOLS: frozenset[str] = frozenset({"write_file", "call_skill_tool"})

# Higher consecutive-repeat tolerance for mutating tools. Default 4 vs the
# read/inspect default of LOOP_REPEAT_LIMIT (2).
LOOP_REPEAT_LIMIT_MUTATING = int(os.getenv("LOOP_REPEAT_LIMIT_MUTATING", "4"))


def repeat_limit_for(name: str) -> int:
    """Per-tool consecutive-repeat threshold.

    Mutating tools (file/sandbox writes) get the higher mutating limit; every
    other tool keeps the base LOOP_REPEAT_LIMIT. Pure: no I/O, no state.
    """
    if name in MUTATING_TOOLS:
        return LOOP_REPEAT_LIMIT_MUTATING
    return LOOP_REPEAT_LIMIT


def result_hash(result: str | None) -> str:
    """Short stable hash of a tool result (for loop disambiguation). None/'' -> ''."""
    if not result:
        return ""
    return hashlib.sha1(str(result).encode("utf-8", "replace")).hexdigest()[:12]


def call_signature(name: str, args: dict, result: str | None = None) -> str:
    """Deterministic signature for a tool call.

    Argument key order must never change the signature, so we sort keys. Values
    that are not JSON-serializable fall back to str() rather than raising — the
    detector must never crash the executor.

    When `result` is given, its hash is folded in so a same-name/same-args call
    with a CHANGING result is a DISTINCT signature (progress, not a loop).
    Back-compatible: result=None reproduces the old (name, args) signature.
    """
    try:
        norm = json.dumps(args or {}, sort_keys=True, default=str)
    except Exception:
        norm = str(args)
    base = f"{name}::{norm}"
    rh = result_hash(result)
    return f"{base}::{rh}" if rh else base


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

    def record(self, signature: str, repeat_limit: int | None = None) -> bool:
        self._sigs.append(signature)
        return self.should_break(repeat_limit=repeat_limit)

    def reason(self) -> str:
        return self._reason

    def should_break(self, repeat_limit: int | None = None) -> bool:
        # The repeat threshold may be overridden per-call (e.g. a higher
        # tolerance for mutating tools). The cycle window/threshold below is
        # deliberately NOT overridden — cycle detection stays as-is.
        repeat_n = self.repeat_limit if repeat_limit is None else repeat_limit
        if repeat_n < 1:
            return False
        sigs = self._sigs
        n = len(sigs)
        if n < repeat_n:
            return False

        # 1) Immediate consecutive repeat (uses the possibly-overridden threshold).
        tail = sigs[-repeat_n:]
        if len(set(tail)) == 1:
            self._reason = "repeat"
            return True

        # 2) No-progress cycle. Look at the recent window; if it cycles a small
        # set of signatures and nothing in the last `repeat_limit` steps is a
        # FIRST-time signature (i.e. no genuine progress), treat it as a loop.
        window = sigs[-self.cycle_window :]
        if len(window) >= 2 * self.repeat_limit:
            distinct = set(window)
            # Small alternating/cycling set: distinct count is at most repeat_limit
            # and every recent step was already seen earlier in the window.
            if len(distinct) <= self.repeat_limit:
                recent = sigs[-self.repeat_limit :]
                earlier = set(sigs[: -self.repeat_limit])
                if all(s in earlier for s in recent):
                    self._reason = "cycle"
                    return True

        return False
