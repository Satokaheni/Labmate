# services/orchestrator/finalize_revision.py
"""Pure helpers for the opt-in revise-before-deliver gate.

No I/O, no model calls, no graph imports — these are deterministic functions so
the side-effect guard, the revision cap, and the empty/error skips are
exhaustively unit-testable. The graph's `revise` node owns the single (bounded)
model call and reads these to decide whether to make it.
"""
from __future__ import annotations


def should_revise(
    final_answer: str,
    *,
    had_side_effects: bool,
    attempts: int,
    max_attempts: int,
    errored: bool,
) -> bool:
    """Return True iff it is safe and useful to make ONE more revision pass.

    Skip (return False) when:
      - there is no visible final answer (nothing to revise),
      - the run errored / aborted (don't paper over a failure),
      - side-effecting tools already ran (revising after actions is unsafe),
      - the revision cap is reached (attempts >= max_attempts) — this also makes
        the gate idempotent: once a pass has run, attempts==1 blocks a replay.
    """
    if errored:
        return False
    if had_side_effects:
        return False
    if final_answer is None or not final_answer.strip():
        return False
    if max_attempts <= 0:
        return False
    if attempts >= max_attempts:
        return False
    return True


def build_revision_prompt(task: str, answer: str) -> str:
    """Build the single revision prompt.

    Re-reads the draft answer against the original task. Crucially it permits
    returning the answer UNCHANGED so a correct answer is not gratuitously
    rewritten (no forced edit). Deterministic for a given (task, answer).
    """
    return (
        "You are reviewing a draft answer before it is delivered to the user.\n"
        "Re-read it against the original request. Check for: incompleteness "
        "(did it fully address the request?), unsupported claims, and fabricated "
        "or made-up facts.\n"
        "If the draft is already correct and complete, return it UNCHANGED. "
        "Otherwise return a corrected version. Reply with ONLY the final answer "
        "text — no preamble, no explanation of what you changed.\n\n"
        f"Original request:\n{task}\n\n"
        f"Draft answer:\n{answer}"
    )
