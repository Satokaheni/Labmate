"""Completion guard — PURE heuristics that reconcile the ``ok`` flag with the
final answer text.

The live A/B (eval/reports/ab_agentic_fix_loop_report.md §4.5) surfaced two
honesty wrinkles where ``ok`` disagreed with the answer:

  1. A read-only skill returned a "file too large / provide a snippet" PUNT and
     it was finalized as ``ok=True`` (false-positive success).
  2. An answer asserted "I fixed it" while ``ok`` was wrong / the claim was not
     backed by a passing test run.

These three side-effect-free functions encode the reconciliation policy so the
orchestrator wiring stays thin and the rule is unit-testable without a model.

  - is_punt_answer(text)  -> a terminal punt ("too large", "could not", ...).
                             A punt MUST NOT be ok=True.
  - asserts_success(text) -> a success claim ("I fixed", "tests now pass", ...).
                             Such a claim must be backed by a passing run_tests
                             this run, else ok is downgraded with a caveat.
  - reconcile_ok(ok, answer, *, tests_passed) -> the corrected (ok, note).

All matching is case-insensitive on a whitespace-normalized copy of the text;
the input string is never mutated and nothing is logged or emitted here.
"""
from __future__ import annotations

import re

# Terminal-punt phrases. A trailing-period/space variation is handled by the
# normalization + substring test below, so list the bare phrase only.
_PUNT_PHRASES: tuple[str, ...] = (
    "too large",
    "is too long",
    "too long",
    "unable to",
    "could not",
    "couldn't",
    "cannot process",
    "can't process",
    "provide a snippet",
    "provide a smaller",
    "please break",
    "break the file",
    "break it into",
    "smaller snippet",
    "smaller portion",
    "smaller part",
)

# Success-claim phrases.
_SUCCESS_PHRASES: tuple[str, ...] = (
    "i fixed",
    "i have fixed",
    "the bug is fixed",
    "bug is fixed",
    "resolved the bug",
    "i resolved",
    "tests now pass",
    "now passing",
    "all tests pass",
    "tests pass",
    "tests are now passing",
)


def _normalize(text: str) -> str:
    """Lower-case and collapse whitespace so phrase matching is robust to
    casing and inter-word spacing/newlines. Pure — returns a new string.
    """
    return re.sub(r"\s+", " ", str(text)).strip().lower()


def is_punt_answer(text: str) -> bool:
    """True if ``text`` is a terminal punt (the agent gave up / asked for a
    smaller input) rather than a real answer. Such an answer must not be ok=True.
    """
    norm = _normalize(text)
    if not norm:
        return False
    return any(p in norm for p in _PUNT_PHRASES)


def asserts_success(text: str) -> bool:
    """True if ``text`` claims the work succeeded (a fix landed / tests pass).
    Such a claim must be backed by a passing run_tests this run before ok=True.
    """
    norm = _normalize(text)
    if not norm:
        return False
    return any(p in norm for p in _SUCCESS_PHRASES)


def reconcile_ok(ok: bool, answer: str, *, tests_passed: bool) -> tuple[bool, str]:
    """Reconcile ``ok`` with the final ``answer``.

    Returns ``(corrected_ok, note)``:

      * If the answer is a PUNT (is_punt_answer) -> ``ok=False``. A punt is never
        a success regardless of the incoming flag.
      * Else if the answer asserts success but NO passing run_tests occurred this
        run (``tests_passed is False``) -> downgrade ``ok`` to ``False`` with an
        honesty note (gate the CLAIM, not just the flag).
      * Otherwise unchanged.

    ``note`` is the empty string when nothing changed; otherwise a short honesty
    note the CALLER appends to its summary. The summary is never mutated here.
    Only a real downgrade (an incoming ``ok=True`` turned False) yields a note —
    a punt whose ``ok`` was already False needs no caveat.
    """
    if is_punt_answer(answer):
        note = "" if not ok else (
            "[completion-guard: answer is a punt (could not complete / "
            "input too large), so this is NOT a success]"
        )
        return False, note

    if asserts_success(answer) and not tests_passed:
        note = "" if not ok else (
            "[completion-guard: success was claimed but NOT verified by a "
            "passing test run this turn]"
        )
        return False, note

    return ok, ""


def reconcile_final_answer(
    ok: bool,
    error: str | None,
    answer: str,
    *,
    tests_passed: bool = False,
) -> tuple[bool, str | None, str]:
    """Reconcile the *rendered* final answer (post-summarizer) with ``ok``/``error``.

    This is the THIRD reconciliation seam (after the skill-first raw-output and the
    ReAct finish seams). The punt wording is produced downstream by the final-answer
    summarizer / stream_final_answer, so the user-facing answer must be re-checked
    before the result leaves the orchestrator.

    Reuses ``reconcile_ok`` (no new phrase list). Returns
    ``(corrected_ok, corrected_error, note)``:

      * If ``reconcile_ok`` downgrades ``ok`` (a punt, or an unverified success
        claim) -> ``corrected_ok=False``. An ``error`` is set so the downgrade
        propagates through the result payload's ``ok`` derivation. A pre-existing
        ``error`` is PRESERVED (never clobbered — keep the real upstream cause);
        only a fresh downgrade (no prior error) gets the honesty note as its error.
      * Otherwise everything is returned unchanged.

    Pure: never mutates its inputs, never logs, never does I/O.
    """
    new_ok, note = reconcile_ok(ok, answer, tests_passed=tests_passed)
    if new_ok == ok:
        # No change (genuine success, empty answer, or already-False input).
        return ok, error, note
    # A downgrade happened. Set an error if none exists; otherwise keep the
    # original (more specific) error.
    new_error = error if error else (note or "final answer reconciled to not-success")
    return new_ok, new_error, note
