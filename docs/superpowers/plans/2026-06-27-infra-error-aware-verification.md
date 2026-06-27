# Infra-Error-Aware Verification-Stop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the verification-stop guard from nudging "run the tests now" forever when the test toolchain cannot run at all (infra error), instead of when tests ran and failed (a real, fixable red).

**Architecture:** `needs_verification` today treats "tests not shown passing" as one undifferentiated state and keeps nudging until `MAX_VERIFY_NUDGES`. In the live A/B that produced ~45 wasted calls because the test tool *errored* (couldn't run) rather than *failed* (ran, red). We add a pure classifier that labels each `run_tests`/`run_bash` result as `ran-and-passed`, `ran-and-failed`, or `infra-error`, track a consecutive-infra-error streak in the loop, and make verification-stop bail honestly once the streak hits a small cap — finishing with a truthful "could not run tests in this environment" note rather than a fabricated or endlessly-nudged completion.

**Tech Stack:** Python 3.11, pytest + pytest-asyncio, pytest-bdd.

## Global Constraints

- Pure helpers (no I/O, no model calls) live in their own module and are unit-testable without a model.
- New env knobs read via `os.getenv` at call time, safe defaults, additive.
- Assert structure, not literal LLM text.
- Do not regress existing verification-stop behavior: edited-nothing → finish; tests passed → finish; cap reached → accept finish.
- Depends on the run_tests result shape `{ok, exit_code, raw_output}` and the skill/exec error envelopes (`{"error": …}`, `skill_unavailable`, `dispatch_failed`, `timeout`). It composes with — does not replace — `MAX_VERIFY_NUDGES`.

---

### Task 1: Classify a test-tool result (ran-passed / ran-failed / infra-error)

**Files:**
- Create: `services/orchestrator/test_outcome.py`
- Test: `tests/services/orchestrator/test_test_outcome.py`

**Interfaces:**
- Produces: `classify_test_attempt(content: str) -> TestOutcome` where `TestOutcome` is a `@dataclass(frozen=True)` with fields `ran: bool`, `passed: bool`, `infra_error: bool`, `reason: str`. Exactly one of `passed` / (`ran and not passed`) / `infra_error` is the "true" state; `infra_error` implies `not ran`.

`content` is the JSON string stored as the tool result in the loop (what `_run_tests_passed` already parses): either `{"ok", "exit_code", "raw_output"}` (a real run) or `{"error": "..."}` (a dispatch/exec failure), or a raw bash blob.

- [ ] **Step 1: Write the failing tests**

Create `tests/services/orchestrator/test_test_outcome.py`:

```python
from services.orchestrator.test_outcome import classify_test_attempt, TestOutcome


def test_passing_run():
    o = classify_test_attempt('{"ok": true, "exit_code": 0, "raw_output": "3 passed"}')
    assert o.ran and o.passed and not o.infra_error


def test_failing_run():
    o = classify_test_attempt('{"ok": false, "exit_code": 1, "raw_output": "1 failed, 2 passed"}')
    assert o.ran and not o.passed and not o.infra_error


def test_infra_error_explicit_error_key():
    o = classify_test_attempt('{"error": "no test runner available"}')
    assert o.infra_error and not o.ran and not o.passed
    assert "no test runner" in o.reason


def test_infra_error_skill_unavailable_in_raw_output():
    o = classify_test_attempt('{"ok": false, "exit_code": 1, "raw_output": "skill_unavailable: no tool"}')
    assert o.infra_error and not o.ran


def test_infra_error_timeout():
    o = classify_test_attempt('{"ok": false, "exit_code": 1, "raw_output": "timeout"}')
    assert o.infra_error


def test_infra_error_exec_run_pytest_blocked():
    o = classify_test_attempt(
        '{"ok": false, "exit_code": 1, "raw_output": '
        '"exec_run: this command looks like code execution and is not allowed"}')
    assert o.infra_error


def test_no_tests_collected_is_infra_not_a_real_fail():
    o = classify_test_attempt('{"ok": false, "exit_code": 1, "raw_output": "no tests ran in 0.01s"}')
    assert o.infra_error


def test_garbage_is_infra_error():
    o = classify_test_attempt("not json at all")
    assert o.infra_error
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/services/orchestrator/test_test_outcome.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the classifier**

Create `services/orchestrator/test_outcome.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/services/orchestrator/test_test_outcome.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/test_outcome.py tests/services/orchestrator/test_test_outcome.py
git commit -m "feat(orchestrator): classify test attempts (ran-passed/ran-failed/infra-error)"
```

---

### Task 2: Verification-stop bails on an infra-error streak

Extend the verification-stop policy: if the last N consecutive verification attempts were infra errors, stop nudging (nudging cannot help a broken toolchain) and let the loop finish honestly.

**Files:**
- Modify: `services/orchestrator/verification_stop.py`
- Test: `tests/services/orchestrator/test_verification_stop.py`

**Interfaces:**
- Produces:
  - `MAX_VERIFY_INFRA_ERRORS` default `2` (read via env `MAX_VERIFY_INFRA_ERRORS`).
  - `needs_verification(edited_files, tests_passed, nudges_used, max_nudges, *, infra_error_streak: int = 0, max_infra_errors: int | None = None) -> bool` — backward-compatible (new keyword-only params default to "no infra info" → old behavior). Returns False (stop nudging) when `infra_error_streak >= max_infra_errors`.
  - `build_infra_unverified_note(edited_files: set[str], reason: str) -> str` — an honest finish annotation for when tests could not be run.

- [ ] **Step 1: Write the failing tests**

Add to `tests/services/orchestrator/test_verification_stop.py`:

```python
from services.orchestrator.verification_stop import (
    needs_verification,
    build_infra_unverified_note,
    MAX_VERIFY_INFRA_ERRORS,
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/services/orchestrator/test_verification_stop.py -k "infra" -v`
Expected: FAIL — new symbols/params do not exist.

- [ ] **Step 3: Implement**

In `services/orchestrator/verification_stop.py`, add at the top (after the module docstring/imports):

```python
import os


def _max_infra_errors_default() -> int:
    try:
        return int(os.getenv("MAX_VERIFY_INFRA_ERRORS", "2"))
    except ValueError:
        return 2


MAX_VERIFY_INFRA_ERRORS = _max_infra_errors_default()
```

Replace `needs_verification` with:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/services/orchestrator/test_verification_stop.py -v`
Expected: PASS (old tests + new infra tests).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/verification_stop.py tests/services/orchestrator/test_verification_stop.py
git commit -m "feat(verify): bail honestly on a verification infra-error streak"
```

---

### Task 3: Wire the infra streak into the ReAct loop finish branch

Track consecutive infra errors from `run_tests`/`run_bash` results and pass the streak into `needs_verification`. When the guard bails because the toolchain is broken (not because the budget ran out), append the honest `build_infra_unverified_note` to the finish summary so the user sees an UNVERIFIED result rather than silence or a fabricated pass.

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py` (the `run_tests`/`run_bash` result handling and the `finish` branch — around lines 988-1044 and the verification-stop check near 805-815)
- Test: `tests/services/orchestrator/features/infra_aware_verification.feature` + `tests/services/orchestrator/test_infra_aware_verification_bdd.py`

**Interfaces:**
- Consumes: `classify_test_attempt` (Task 1), `needs_verification` + `build_infra_unverified_note` (Task 2).
- Produces: loop-local `infra_error_streak: int` updated on every `run_tests`/`run_bash` result (reset to 0 on a `ran` outcome, incremented on `infra_error`); finish summary annotated with the infra note when the guard bails on the streak.

- [ ] **Step 1: Add loop state + streak update**

In `_run_react_loop` (coding_orchestrator.py), initialize alongside the other verification state (near `verify_nudges_used = 0`):

```python
                infra_error_streak = 0
```

Import the classifier at the top of the module:

```python
from .test_outcome import classify_test_attempt
```

After the `run_tests` branch sets `content` (right after the `if _run_tests_passed(content): tests_passed = True` line), add:

```python
                        _outcome = classify_test_attempt(content)
                        if _outcome.infra_error:
                            infra_error_streak += 1
                        elif _outcome.ran:
                            infra_error_streak = 0
                            _last_infra_reason = _outcome.reason
```

Do the same after the `run_bash` branch ONLY when the command was a pytest invocation (so non-test bash does not move the streak):

```python
                        if "pytest" in str(args.get("command", "")):
                            _bash_outcome = classify_test_attempt(content)
                            if _bash_outcome.infra_error:
                                infra_error_streak += 1
                            elif _bash_outcome.ran:
                                infra_error_streak = 0
```

Track the last reason near the other init: `_last_infra_reason = "test toolchain error"`.

- [ ] **Step 2: Pass the streak into the verification-stop check and annotate the finish**

The existing `needs_verification(...)` call is at coding_orchestrator.py **lines ~770-773**:

```python
                        if needs_verification(
                            edited_files, tests_passed,
                            verify_nudges_used, max_verify_nudges,
                        ):
```

Note `max_verify_nudges` is a **local variable** (set at line 531 from `os.getenv("MAX_VERIFY_NUDGES", "2")`), NOT a module constant. Add the `infra_error_streak` keyword to this exact call, and add an infra-bail annotation. Compute `_infra_blocked` BEFORE the call and annotate the finish summary in the `else` (the path taken when verification is NOT needed). Graft into the real control flow — do not duplicate the existing nudge/finish bodies:

```python
                        _infra_blocked = (
                            bool(edited_files)
                            and not tests_passed
                            and infra_error_streak >= MAX_VERIFY_INFRA_ERRORS
                        )
                        if needs_verification(
                            edited_files, tests_passed,
                            verify_nudges_used, max_verify_nudges,
                            infra_error_streak=infra_error_streak,
                        ):
                            verify_nudges_used += 1
                            # ... EXISTING nudge/emit/continue body stays exactly as-is ...
                        else:
                            if _infra_blocked:
                                _summary = (args.get("summary") or "")
                                args["summary"] = (
                                    _summary + "\n\n"
                                    + build_infra_unverified_note(edited_files, _last_infra_reason)
                                ).strip()
                            # ... EXISTING finish/return body stays exactly as-is ...
```

`MAX_VERIFY_INFRA_ERRORS` is the module constant added in Task 2 (import it, below).

Add the imports near the other verification-stop imports:

```python
from .verification_stop import (
    needs_verification,
    build_verify_nudge,
    build_infra_unverified_note,
    MAX_VERIFY_INFRA_ERRORS,
)
```

> Implementer note: read the actual finish/verification-stop block first (lines ~790-820) and graft these additions into the real control flow — the `# ... existing ... path ...` comments mark where the current code already does the continue/return. Do not duplicate the existing logic; only add the streak argument and the infra-note annotation.

- [ ] **Step 3: Write the BDD scenario**

Create `tests/services/orchestrator/features/infra_aware_verification.feature`:

```gherkin
@mocked
Feature: Infra-error-aware verification stop
  The agent must not nudge "run the tests" forever when the test toolchain
  cannot run, and must finish with an honest UNVERIFIED note.

  Scenario: Broken test toolchain yields an honest unverified finish
    Given the test toolchain returns an infra error on every run_tests call
    And the agent edits a file and then calls run_tests twice
    When the agent attempts to finish
    Then the final summary marks the result as unverified
    And the final summary does not claim the tests passed
```

Create `tests/services/orchestrator/test_infra_aware_verification_bdd.py` using the shared `fake_model` fixture and the async-run helper in `tests/conftest.py` (mirror an existing `test_*_bdd.py` in that directory for the harness wiring). The model script: tool_call write_file → tool_call run_tests (returns infra error) → tool_call run_tests (infra error) → finish. Stub `skill_router.execute` to return `{"ok": False, "error": "skill_unavailable", "detail": "no tool"}` so the run_tests branch produces an infra-error result. Assert the returned final summary contains "UNVERIFIED" (case-insensitive) and does not contain "all tests pass".

> Implementer note: copy the fixture/stub setup verbatim from the nearest existing BDD test (e.g. the verification-stop or agentic-fix-loop BDD file) so the orchestrator is constructed identically; only the model script and assertions differ.

- [ ] **Step 4: Run the BDD scenario + the verification suite**

Run: `python -m pytest tests/services/orchestrator/test_infra_aware_verification_bdd.py tests/services/orchestrator/test_verification_stop.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/coding_orchestrator.py tests/services/orchestrator/features/infra_aware_verification.feature tests/services/orchestrator/test_infra_aware_verification_bdd.py
git commit -m "feat(react): track infra-error streak; honest unverified finish on broken toolchain"
```

---

### Task 4: Whole-suite regression gate

- [ ] **Step 1:** Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator -q` — Expected: PASS, no regressions.

---

## Self-Review

- **Spec coverage:** classifier (Task 1) → bail policy (Task 2) → loop wiring + honest finish (Task 3). ✓
- **Backward compatibility:** `needs_verification`'s new params are keyword-only with defaults that reproduce old behavior; existing callers/tests unaffected unless they opt in. ✓
- **Honesty:** `build_infra_unverified_note` never claims success and is asserted against "all tests pass"; composes with the existing `completion_guard.reconcile_*` which already downgrades unverified "I fixed it" claims. ✓
- **Type consistency:** `classify_test_attempt` consumes the same `content` JSON that `_run_tests_passed` already parses; `infra_error implies not ran` invariant holds across the classifier. ✓
- **Interaction note:** this reduces the 45-call thrash by cutting nudges early; it does not change the no-progress / wall-clock breakers, which remain as independent backstops.
