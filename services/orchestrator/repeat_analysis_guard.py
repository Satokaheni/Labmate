"""Guard against re-running a read-only ANALYSIS skill (code-review, critique, …) on a target
the ReAct loop already analyzed this goal. Mirrors load_skill_guard.py, but for call_skill_tool:
a re-review is short-circuited with a steer toward the edit instead of burning another iteration.
Flag-gated (ENABLE_REPEAT_ANALYSIS_GUARD); **default ON** (adopted 2026-07-03 after the c2 A/B:
no regression, cheaper on average, eliminates the budget-exhaustion churn tail). Set =0 to disable.
"""

import os

# Read-only "produce a diagnosis" skills whose RE-invocation on the same target is churn.
# NOT code-sandbox / run_tests — re-running those after an edit is legit verification.
_DEFAULT_ANALYSIS_SKILLS = frozenset({"code-review", "critique", "design-critique"})

# arg fields that commonly carry the analyzed target (best-effort).
_TARGET_ARG_KEYS = ("file", "path", "filename", "target", "file_path")


def analysis_skills() -> frozenset[str]:
    """Guarded analysis skills; override via REPEAT_ANALYSIS_SKILLS=comma,list."""
    raw = os.getenv("REPEAT_ANALYSIS_SKILLS", "")
    if raw.strip():
        return frozenset(s.strip() for s in raw.split(",") if s.strip())
    return _DEFAULT_ANALYSIS_SKILLS


def repeat_analysis_guard_enabled() -> bool:
    return os.getenv("ENABLE_REPEAT_ANALYSIS_GUARD", "1") == "1"


def is_guarded_analysis(skill: str) -> bool:
    return skill in analysis_skills()


def analysis_key(skill: str, arguments: dict) -> str:
    """skill + best-effort target. Same-file re-review → same key; different file → different key."""
    target = ""
    if isinstance(arguments, dict):
        for k in _TARGET_ARG_KEYS:
            v = arguments.get(k)
            if isinstance(v, str) and v.strip():
                target = v.strip()
                break
    return f"{skill}::{target}" if target else skill


def build_analysis_steer(skill: str, key: str) -> dict:
    """Grounded result returned in place of a redundant re-analysis."""
    target = key.split("::", 1)[1] if "::" in key else ""
    where = f" on {target}" if target else ""
    return {
        "name": "call_skill_tool",
        "response": {
            "status": "already_analyzed",
            "message": (
                f"You already ran {skill}{where} this goal and have its findings. "
                f"Do NOT re-review — make the edit now with write_file, then run the tests to "
                f"verify. If you already edited, run the tests rather than reviewing again."
            ),
        },
    }


__all__ = [
    "analysis_skills",
    "repeat_analysis_guard_enabled",
    "is_guarded_analysis",
    "analysis_key",
    "build_analysis_steer",
]
