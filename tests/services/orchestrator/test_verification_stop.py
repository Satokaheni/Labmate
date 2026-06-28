# tests/services/orchestrator/test_verification_stop.py
from __future__ import annotations

from services.orchestrator.verification_stop import needs_verification


def test_no_edits_never_needs_verification():
    # Edited nothing -> guard must not fire, regardless of tests_passed.
    assert needs_verification(set(), tests_passed=False, nudges_used=0, max_nudges=2) is False
    assert needs_verification(set(), tests_passed=True, nudges_used=0, max_nudges=2) is False


def test_edited_without_passing_tests_under_cap_needs_verification():
    assert needs_verification({"src/app.py"}, tests_passed=False, nudges_used=0, max_nudges=2) is True
    assert needs_verification({"src/app.py"}, tests_passed=False, nudges_used=1, max_nudges=2) is True


def test_edited_with_passing_tests_does_not_need_verification():
    assert needs_verification({"src/app.py"}, tests_passed=True, nudges_used=0, max_nudges=2) is False


def test_cap_reached_does_not_need_verification():
    # nudges_used == max_nudges -> stop nudging, accept finish honestly.
    assert needs_verification({"src/app.py"}, tests_passed=False, nudges_used=2, max_nudges=2) is False
    assert needs_verification({"src/app.py"}, tests_passed=False, nudges_used=3, max_nudges=2) is False


def test_zero_cap_never_nudges():
    assert needs_verification({"src/app.py"}, tests_passed=False, nudges_used=0, max_nudges=0) is False


from services.orchestrator.verification_stop import build_verify_nudge


def test_build_verify_nudge_lists_files_sorted_and_is_deterministic():
    files = {"src/b.py", "src/a.py"}
    msg1 = build_verify_nudge(files)
    msg2 = build_verify_nudge(files)
    # Deterministic: same input -> identical output (prefix-cache friendly).
    assert msg1 == msg2
    # Files appear sorted, so set ordering cannot perturb the text.
    assert "src/a.py" in msg1
    assert "src/b.py" in msg1
    assert msg1.index("src/a.py") < msg1.index("src/b.py")


def test_build_verify_nudge_instructs_run_then_fix_then_finish():
    msg = build_verify_nudge({"src/app.py"})
    lowered = msg.lower()
    # Must tell the model to run tests, read failures, fix, and only then finish.
    assert "test" in lowered
    assert "fail" in lowered or "failure" in lowered
    assert "finish" in lowered


def test_build_verify_nudge_handles_empty_set():
    # Defensive: caller only invokes this when files exist, but it must not crash.
    msg = build_verify_nudge(set())
    assert isinstance(msg, str) and msg


from services.orchestrator.verification_stop import (
    MAX_VERIFY_INFRA_ERRORS,
    build_infra_unverified_note,
)


def test_needs_verification_unchanged_without_infra_info():
    # edited, not passed, budget left -> still nudge (old behavior preserved).
    assert needs_verification({"a.py"}, False, 0, 2) is True


def test_needs_verification_stops_on_infra_streak():
    assert needs_verification(
        {"a.py"}, False, 0, 2, infra_error_streak=2, max_infra_errors=2
    ) is False


def test_needs_verification_continues_below_infra_cap():
    assert needs_verification(
        {"a.py"}, False, 0, 2, infra_error_streak=1, max_infra_errors=2
    ) is True


def test_default_infra_cap_value():
    assert MAX_VERIFY_INFRA_ERRORS == 2


def test_build_infra_unverified_note_is_honest():
    note = build_infra_unverified_note({"a.py", "b.py"}, "test toolchain error: skill_unavailable")
    assert "could not" in note.lower() or "unable" in note.lower()
    assert "skill_unavailable" in note
    assert "a.py" in note and "b.py" in note
    # Must NOT claim success.
    assert "all tests pass" not in note.lower()


from services.orchestrator.verification_stop import build_expose_test_nudge


def test_build_expose_test_nudge_steers_toward_failing_not_passing():
    nudge = build_expose_test_nudge({"test_off.py"})
    low = nudge.lower()
    # Tells the agent to RUN the test and that a FAILING result is correct.
    assert "run_tests" in low
    assert "fail" in low
    # Steers AWAY from making it pass (the phrase appears only inside a "do NOT" clause).
    assert "do not" in low and "make it pass" in low
    assert "test_off.py" in nudge


def test_build_expose_test_nudge_is_deterministic():
    a = build_expose_test_nudge({"b.py", "a.py"})
    b = build_expose_test_nudge({"a.py", "b.py"})
    assert a == b  # files sorted -> byte-stable
