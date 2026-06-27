"""Verification-stop guard (hermes pattern) — pure helpers.

The ReAct loop must not accept a ``finish`` that claims completion after the
agent edited code but never ran tests and saw them pass. These two
side-effect-free functions encode that policy so the loop wiring stays thin and
the rule is unit-testable without a model.

See docs/superpowers/plans/2026-06-26-verification-stop-guard.md
"""
from __future__ import annotations


def needs_verification(
    edited_files: set[str],
    tests_passed: bool,
    nudges_used: int,
    max_nudges: int,
) -> bool:
    """Should the loop inject another verification nudge instead of finishing?

    True iff the agent edited at least one file, has NOT shown a passing test
    run, and we have not yet spent the nudge budget. False otherwise — which
    covers: edited nothing (finish as today), tests already passed (finish as
    today), and cap reached (accept finish, summary annotated honestly by the
    caller).
    """
    if not edited_files:
        return False
    if tests_passed:
        return False
    return nudges_used < max_nudges


def build_verify_nudge(edited_files: set[str]) -> str:
    """A synthetic user message that forces the agent back into the loop.

    Deterministic (files sorted) so the appended tail stays byte-stable across
    runs. Mirrors the hermes verification-stop nudge: run the verification
    command, read any failure, repair the code, re-run, finish only on pass.
    """
    files = ", ".join(sorted(edited_files)) if edited_files else "the files you changed"
    return (
        f"You edited {files} but you have not shown that the tests pass. "
        "Call the run_tests tool now to run the suite (it returns the raw "
        "pass/fail output). Read any failure, fix the code, and re-run. "
        "Only call finish once run_tests reports the tests actually pass."
    )
