"""Case set + fixture bodies for the local in-process eval runner.

COPIED (not imported) from eval/seq_ab/run_seq_ab.py: that module imports
`redis` at the top of the file (it drives goals through a standalone Redis
stream — see eval/seq_ab/local_tool_responder.py's module docstring for why,
and .superpowers/sdd/local-eval-harness-brief.md for the decision), and the
whole point of eval/local/ is a Redis-free harness. Importing run_seq_ab would
drag `redis` into `python -m eval.local.run_local_eval --help`, defeating the
"resolves without a model / without redis" requirement. So the CASE dicts and
FIXTURES bodies are copied here verbatim instead.

Keep this file in sync by eye if eval/seq_ab/run_seq_ab.py's CASES/FIXTURES
change — there is no automated sync (the two harnesses are intentionally
decoupled; eval/seq_ab/ is kept only as a stale reference).
"""

from __future__ import annotations

# Fixtures are reset before each trial so a prior run's fix doesn't leak.
FIXTURES: dict[str, str] = {
    "/workspace/ab_factorial.py": (
        'def factorial(n):\n    """Return n! (product of 1..n). 0! == 1."""\n'
        "    result = 1\n    for i in range(1, n):          # bug: excludes n\n"
        "        result *= i\n    return result\n"
    ),
    "/workspace/ab_buggy.py": (
        'def average(nums):\n    """Return the arithmetic mean of a list of numbers."""\n'
        "    total = 0\n    for x in nums:\n        total += x\n"
        "    return total / len(nums) + 1    # bug: stray + 1\n"
    ),
    "/workspace/ab_off.py": (
        'def last_index(seq, target):\n    """Return the index of the LAST occurrence of target, or -1 if absent."""\n'
        "    for i in range(len(seq)):\n        if seq[i] == target:\n"
        "            return i               # bug: returns FIRST match, not last\n    return -1\n"
    ),
    "/workspace/ab_multi.py": (
        "def normalize(xs):\n"
        '    """Scale a list of numbers to sum to 1.0. Empty list -> []."""\n'
        "    total = 0\n"
        "    for x in xs:\n"
        "        total = x            # bug 1: assignment, not accumulation\n"
        "    return [x / total for x in xs]   # bug 2: no empty-list guard (ZeroDivisionError)\n"
    ),
}

CASES: list[dict[str, str]] = [
    {
        "id": "c1_testgen_review_fix",
        "kind": "compound",
        "task": "Generate unit tests for the factorial function in /workspace/ab_factorial.py, run them, find and fix the bug, and re-run until they pass.",
    },
    {
        "id": "c2_review_fix",
        "kind": "compound",
        "task": "Review /workspace/ab_buggy.py for bugs, then fix the code.",
    },
    {
        "id": "c3_bug_then_test",
        "kind": "compound",
        "task": "Find the bug in /workspace/ab_off.py and write a unit test that exposes it.",
    },
    {
        "id": "c6_multiedit_fix",
        "kind": "compound",
        "task": "Fix the two bugs in /workspace/ab_multi.py (the running total and the empty-list crash), then write and run a test that proves normalize works and handles [].",
    },
    {
        "id": "c4_single_review",
        "kind": "control_single",
        "task": "Find bugs in /workspace/ab_buggy.py.",
    },
    {"id": "c5_trivial", "kind": "control_trivial", "task": "What is 2+2? Reply in one sentence."},
]


def cases_for(workspace_root: str | None = None) -> list[dict[str, str]]:
    """Return CASES with the fixture-path prefix rebased onto `workspace_root`.

    The task strings hardcode the RunPod "/workspace/ab_*.py" paths; on any other
    host (e.g. a Mac client) the harness runs against a different WORKSPACE_PATH,
    so the paths the MODEL is told to edit must match where reset_fixtures() wrote
    the files. When `workspace_root` is None or "/workspace" this is a no-op, so
    RunPod runs are byte-identical (baseline comparability preserved).
    """
    if not workspace_root or workspace_root == "/workspace":
        return [dict(c) for c in CASES]
    root = workspace_root.rstrip("/")
    return [{**c, "task": c["task"].replace("/workspace", root)} for c in CASES]


def reset_fixtures(workspace_root: str | None = None) -> None:
    """Write each FIXTURES body back to disk, restoring the buggy starting state.

    Call before EACH trial so a previous trial's edit doesn't leak into the next.
    `workspace_root`, if given, replaces the "/workspace" prefix (useful when the
    local harness runs against a different WORKSPACE_PATH than the RunPod default).
    Pair with cases_for(workspace_root) so the task strings point at the same paths.
    """
    import os

    for path, body in FIXTURES.items():
        target = path
        if workspace_root:
            target = os.path.join(workspace_root, os.path.relpath(path, "/workspace"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as f:
            f.write(body)
