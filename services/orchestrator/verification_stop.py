"""Verification-stop guard (hermes pattern) — pure helpers.

The ReAct loop must not accept a ``finish`` that claims completion after the
agent edited code but never ran tests and saw them pass. These two
side-effect-free functions encode that policy so the loop wiring stays thin and
the rule is unit-testable without a model.

See docs/superpowers/plans/2026-06-26-verification-stop-guard.md
"""
from __future__ import annotations

import os


def _max_infra_errors_default() -> int:
    try:
        return int(os.getenv("MAX_VERIFY_INFRA_ERRORS", "2"))
    except ValueError:
        return 2


MAX_VERIFY_INFRA_ERRORS = _max_infra_errors_default()


def needs_verification(
    edited_files: set[str],
    tests_passed: bool,
    nudges_used: int,
    max_nudges: int,
    *,
    infra_error_streak: int = 0,
    max_infra_errors: int | None = None,
) -> bool:
    """Should the loop inject another verification nudge instead of finishing?

    True iff the agent edited >=1 file, has NOT shown a passing run, has nudge
    budget left, AND the test toolchain is not provably broken. A run of
    consecutive infra errors (the suite could not execute at all) means nudging
    cannot help, so we stop and let the caller finish honestly.
    """
    if not edited_files:
        return False
    if tests_passed:
        return False
    cap = MAX_VERIFY_INFRA_ERRORS if max_infra_errors is None else max_infra_errors
    if cap and infra_error_streak >= cap:
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


def build_infra_unverified_note(edited_files: set[str], reason: str) -> str:
    """Honest finish annotation when tests could not be run (infra failure).

    Deterministic (files sorted). Explicitly states the work is UNVERIFIED and
    why — never claims success.
    """
    files = ", ".join(sorted(edited_files)) if edited_files else "the files changed"
    return (
        f"NOTE: I edited {files} but could NOT verify the change — the test "
        f"toolchain failed to run ({reason}). The edits are therefore UNVERIFIED; "
        "please run the test suite manually to confirm."
    )
