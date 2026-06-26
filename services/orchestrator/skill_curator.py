"""
Skill curator — PROPOSAL-ONLY, background, best-effort.

Observes recent SUCCESSFUL multi-tool sequences and DRAFTS candidate skills into
a `.proposed/` staging tier for HUMAN review. It NEVER generates a runnable MCP
server and NEVER auto-activates a skill (SkillRunner.discover skips `.proposed`).

CRITICAL: never write to stdout (this runs inside the orchestrator process whose
stdout carries JSON-RPC / event data). Log to stderr only. Every public entry
point is best-effort: failures are caught + logged at DEBUG and never propagate
into goal execution.
"""
from __future__ import annotations

import logging
import os
from collections import deque
from dataclasses import dataclass

log = logging.getLogger("skill_curator")  # -> stderr via host handlers


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLE_SKILL_CURATOR = _flag("ENABLE_SKILL_CURATOR", "0")
CURATOR_INTERVAL_HOURS = float(os.getenv("CURATOR_INTERVAL_HOURS", "168"))
CURATOR_MIN_IDLE_HOURS = float(os.getenv("CURATOR_MIN_IDLE_HOURS", "2"))
CURATOR_MIN_SEQUENCE_LEN = int(os.getenv("CURATOR_MIN_SEQUENCE_LEN", "2"))
CURATOR_RECENT_BUFFER = int(os.getenv("CURATOR_RECENT_BUFFER", "64"))

PROPOSED_DIRNAME = ".proposed"
SKILL_PROPOSED_EVENT = "skill.proposed"


@dataclass(frozen=True)
class CapturedSequence:
    """A completed goal and the ordered tools it used."""
    name: str               # kebab-case candidate skill name
    goal: str               # the user goal text
    tools: tuple[str, ...]  # ordered tool/skill names used
    ok: bool                # did the goal succeed
    ts: float               # epoch seconds at completion


class RecentSequences:
    """Bounded ring buffer of recent SUCCESSFUL multi-tool sequences.

    record() silently drops failed or too-short sequences so the curator only
    ever drafts from genuine, repeatable successes.
    """

    def __init__(self, maxlen: int = CURATOR_RECENT_BUFFER) -> None:
        self._buf: deque[CapturedSequence] = deque(maxlen=maxlen)

    def record(self, seq: CapturedSequence) -> None:
        if not seq.ok:
            return
        if len(seq.tools) < CURATOR_MIN_SEQUENCE_LEN:
            return
        self._buf.append(seq)

    def snapshot(self) -> list[CapturedSequence]:
        return list(self._buf)


def should_run_now(
    state,
    now: float,
    interval_hours: float,
    min_idle_hours: float,
    idle_for_s: float,
) -> bool:
    """PURE gate: True iff the curator should run this cycle.

    Opens only when ALL hold:
      - not paused
      - at least ``interval_hours`` have elapsed since ``state.last_run_at``
      - the host has been idle for at least ``min_idle_hours``
    """
    if getattr(state, "paused", False):
        return False
    if (now - state.last_run_at) < interval_hours * 3600.0:
        return False
    if idle_for_s < min_idle_hours * 3600.0:
        return False
    return True
