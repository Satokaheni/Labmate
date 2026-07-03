"""Trivial 'no-op' floor for seq_ab: the pass-rate of an agent that finishes
immediately with no work. It is the absolute floor every real run must beat.

All current case kinds require the agent to DO something (review/fix/test, or
answer), so a no-op scores 0.0 everywhere. Reported next to every delta so a
'2/3' is visibly above the do-nothing floor. (The other reference — the cheap
always-skill_first arm — is the baseline arm carried by compare.compare_runs.)
"""

NOOP_PASS_BY_KIND: dict[str, float] = {
    "compound": 0.0,
    "control_single": 0.0,
    "control_trivial": 0.0,
}


def noop_floor(kind: str) -> float:
    return NOOP_PASS_BY_KIND.get(kind, 0.0)
