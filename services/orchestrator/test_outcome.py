"""Classify a run_tests / run_bash result into ran-passed / ran-failed / infra-error.

A red test run ("ran and failed") is a fixable signal — the agent should keep
iterating. An infra error ("could not run") is NOT fixable by nudging the model
to "run the tests again", so the verification-stop guard must bail honestly
instead of looping. These pure helpers encode that distinction.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

# Substrings in raw output that mean the suite never actually executed.
_INFRA_MARKERS = (
    "skill_unavailable",
    "dispatch_failed",
    "no test runner",
    "no bash runner",
    "not allowed",          # exec_run sandbox-bypass rejection
    "timed out",
    "timeout",
    "no tests ran",         # pytest collected nothing -> path/env problem, not a real fail
    "no tests collected",
    "command not found",
    "modulenotfounderror",  # env not set up -> infra, not a logic failure
)


@dataclass(frozen=True)
class TestOutcome:
    ran: bool
    passed: bool
    infra_error: bool
    reason: str


def _infra(reason: str) -> TestOutcome:
    return TestOutcome(ran=False, passed=False, infra_error=True, reason=reason)


def classify_test_attempt(content: str) -> TestOutcome:
    try:
        data = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return _infra("unparseable test result")

    if not isinstance(data, dict):
        return _infra("unexpected test result shape")

    # Explicit dispatch/exec error envelope.
    err = data.get("error")
    if err:
        return _infra(str(err))

    raw = str(data.get("raw_output") or data.get("stdout") or "")
    low = raw.lower()

    ok = bool(data.get("ok"))
    if ok and "infra" not in low:
        return TestOutcome(ran=True, passed=True, infra_error=False, reason="tests passed")

    # ok is False (or absent). Decide infra-error vs genuine red.
    for marker in _INFRA_MARKERS:
        if marker in low:
            return _infra(f"test toolchain error: {marker}")

    return TestOutcome(ran=True, passed=False, infra_error=False, reason="tests failed")
