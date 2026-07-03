"""Provenance header for eval result files: model + git sha + capture time + the
tracked env-flag snapshot. Lets a reader (and compare_provenance) tell whether two
result files were captured under the SAME conditions or differ on >1 axis.

Pure builders take all inputs as arguments (no wall-clock, no subprocess) so they are
deterministic and testable. current_git_sha() is the only impure helper.
"""

from __future__ import annotations

import subprocess

# The behavioral flags worth snapshotting (audit spec, Part A2). Extend as flags land.
TRACKED_FLAGS: list[str] = [
    "SEQUENCING_MODE",
    "ROUTE_EDIT_TO_REACT",
    "LABMATE_REFUND_REPEAT_LOAD_SKILL",
    "LOOP_REPEAT_LIMIT_MUTATING",
    "LABMATE_MAX_ITERATIONS_EDIT",
    "LABMATE_TOOL_RESULT_BUDGET",
    "LABMATE_GOAL_DEADLINE_S",
    "LABMATE_NOPROGRESS_LIMIT",
    "MAX_VERIFY_NUDGES",
    "MAX_GOAL_ATTEMPTS",
    "ENABLE_CONDITIONAL_GATES",
    "ENABLE_MESSAGE_REPAIR",
    "ENABLE_FINALIZE_REVISION",
    "ENABLE_LOOP_CHECKPOINT",
    "ENABLE_ROUTING_PREGATE",
]


def capture_env_flags(environ, names: list[str] = TRACKED_FLAGS) -> dict:
    """Snapshot only the tracked flags that are actually set in `environ`."""
    return {k: environ[k] for k in names if k in environ}


def build_provenance(model: str, git_sha: str, captured_at: str, env: dict) -> dict:
    return {"model": model, "git_sha": git_sha, "captured_at": captured_at, "env": env}


def compare_provenance(a: dict, b: dict) -> list[str]:
    """Warnings when two provenance headers differ on an axis that should be frozen."""
    warnings = []
    for axis in ("model", "git_sha"):
        if a.get(axis) != b.get(axis):
            warnings.append(f"{axis} differs: {a.get(axis)!r} != {b.get(axis)!r}")
    return warnings


def current_git_sha() -> str:
    """Short HEAD sha, or 'unknown' if git is unavailable."""
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"
