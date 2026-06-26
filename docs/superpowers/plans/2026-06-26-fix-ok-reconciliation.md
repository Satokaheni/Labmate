# OK/Answer Reconciliation (Completion Guard) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the orchestrator's `ok` flag from disagreeing with the final answer — a "file too large / could not / provide a snippet" punt must never be `ok=True`, and an "I fixed it" claim must be downgraded to `ok=False` (with an honesty caveat) unless a passing `run_tests` was actually observed this run.

**Architecture:** Add one PURE module `services/orchestrator/completion_guard.py` with three side-effect-free functions (`is_punt_answer`, `asserts_success`, `reconcile_ok`). Wire it into exactly two seams: (a) `AsyncOrchestrator._run_skill_first`, where a single-skill goal's `ok`+answer are finalized (this is where the false-positive `ok=True` PUNT entered), and (b) `AsyncOrchestrator._run_react_loop`'s `finish` branch, reusing the verification-stop guard's existing `tests_passed` signal as `reconcile_ok`'s `tests_passed` argument so an unverified success CLAIM is gated, not just the flag. Additive only; no existing return shape changes except `ok`/`summary` values in the two failure shapes the report flagged.

**Tech Stack:** Python 3.11, pytest, pytest-asyncio, pytest-bdd (respx-backed `fake_model` HTTP seam), `unittest.mock`.

## Global Constraints

- stdout is sacred in MCP servers — never `print()`/`console.log()`; use `logging` to stderr. (completion_guard is pure: no I/O, no logging needed.)
- Every `litellm.acompletion` / `acompletion_with_failover` call sets `extra_body={"thinking_budget_tokens": ...}` and `api_key="not-needed"`. **This change adds NO model calls** — all three functions are pure string heuristics.
- Python files `snake_case.py`; Python functions `snake_case`; Python classes `PascalCase`.
- Tests live in `tests/` mirroring `services/`; `@pytest.mark.asyncio` on async tests; pytest + pytest-asyncio only.
- Assert structure, not literal model text — but the completion_guard functions are deterministic pure heuristics, so their unit tests assert exact booleans/returns on fixed inputs (that is the whole point of extracting them).
- BDD contract: feature at `tests/services/orchestrator/features/<slug>.feature` tagged `@mocked`; step defs at `tests/services/orchestrator/test_<slug>_bdd.py`; scenarios bound via `scenarios("features/<slug>.feature")`; async orchestrator code driven through the `run_async` helper from `tests/conftest.py`.
- Additive + regression-safe: a genuinely-completed goal whose answer is honest, or whose "I fixed it" claim is backed by a passing `run_tests`, MUST stay `ok=True` unchanged.
- Do NOT touch `core/`, `tools/`, legacy `main.py`. Do NOT reference the Discord connector.

---

## File Map

| File | Responsibility | Create / Modify |
|---|---|---|
| `services/orchestrator/completion_guard.py` | PURE heuristics: `is_punt_answer`, `asserts_success`, `reconcile_ok` | **Create** |
| `services/orchestrator/coding_orchestrator.py` | Wire-in (a): `_run_skill_first` return path. Wire-in (b): `_run_react_loop` `finish` branch (reuse `tests_passed`) | **Modify** |
| `tests/services/orchestrator/test_completion_guard.py` | Exhaustive pure unit tests for all three functions | **Create** |
| `tests/services/orchestrator/test_coding_orchestrator.py` | Wire-in unit tests: skill-first punt downgrade; react finish claim-gating; regressions | **Modify (append)** |
| `tests/services/orchestrator/features/ok_reconciliation.feature` | Gherkin `@mocked`: punt → `ok=False`; unverified "I fixed it" → `ok=False` + caveat | **Create** |
| `tests/services/orchestrator/test_ok_reconciliation_bdd.py` | pytest-bdd step defs binding the feature | **Create** |

**Interfaces produced by Task 1 (every later task consumes these EXACT signatures):**

```python
# services/orchestrator/completion_guard.py
def is_punt_answer(text: str) -> bool: ...
def asserts_success(text: str) -> bool: ...
def reconcile_ok(ok: bool, answer: str, *, tests_passed: bool) -> tuple[bool, str]: ...
```

`reconcile_ok` returns `(corrected_ok, note)`. `note` is `""` when nothing changed; otherwise a short honesty note appended by the caller to the summary. The function NEVER mutates the summary itself — the caller decides how to surface `note`.

---

### Task 1: Pure completion_guard module + exhaustive unit tests

**Files:**
- Create: `services/orchestrator/completion_guard.py`
- Test: `tests/services/orchestrator/test_completion_guard.py`

**Interfaces:**
- Consumes: nothing (leaf module, stdlib only).
- Produces: `is_punt_answer(text: str) -> bool`, `asserts_success(text: str) -> bool`, `reconcile_ok(ok: bool, answer: str, *, tests_passed: bool) -> tuple[bool, str]` (see File Map for the contract).

- [ ] **Step 1: Write the failing unit tests**

Create `tests/services/orchestrator/test_completion_guard.py`:

```python
# tests/services/orchestrator/test_completion_guard.py
"""Pure unit tests for the completion_guard heuristics.

These functions are deterministic string heuristics (no model, no I/O), so
unlike model-output tests we assert exact booleans/returns on fixed inputs.
"""
from __future__ import annotations

import pytest

from services.orchestrator.completion_guard import (
    is_punt_answer,
    asserts_success,
    reconcile_ok,
)


# ── is_punt_answer ────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text",
    [
        "I couldn't analyze the file because it is too large. Please provide a smaller snippet.",
        "The file is too long for me to process; break it into smaller parts.",
        "I was unable to complete the task.",
        "I could not find the bug.",
        "Cannot process this input.",
        "Please break the file into smaller pieces and try again.",
        "Provide a snippet and I will help.",
        "Please provide a smaller portion of the code.",
        "  COULDN'T read the file — IT IS TOO LARGE.  ",  # case/space insensitive
    ],
)
def test_is_punt_answer_true_for_terminal_punts(text):
    assert is_punt_answer(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "2 + 2 = 4",
        "I fixed the off-by-one bug and the tests now pass.",
        "Here is the refactored factorial function.",
        "The function returns the square of a number.",
        "",
        "Done.",
        "I reviewed the file and found three potential issues.",  # honest non-punt
    ],
)
def test_is_punt_answer_false_for_real_answers(text):
    assert is_punt_answer(text) is False


# ── asserts_success ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text",
    [
        "I fixed the bug.",
        "The bug is fixed.",
        "All tests pass.",
        "The tests now pass.",
        "Tests are now passing.",
        "I resolved the bug in factorial.",
        "I have fixed the off-by-one error and all tests pass.",
        "  THE BUG IS FIXED.  ",  # case/space insensitive
    ],
)
def test_asserts_success_true_for_success_claims(text):
    assert asserts_success(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "2 + 2 = 4",
        "I reviewed the file for bugs and found a potential null deref.",
        "Here are the unit tests I generated.",
        "I could not fix the bug.",
        "The tests are failing.",
        "",
    ],
)
def test_asserts_success_false_for_neutral_or_negative(text):
    assert asserts_success(text) is False


# ── reconcile_ok ──────────────────────────────────────────────────────────────

def test_reconcile_punt_downgrades_ok_true_to_false():
    ok, note = reconcile_ok(
        True,
        "I couldn't analyze the file because it is too large. Provide a smaller snippet.",
        tests_passed=False,
    )
    assert ok is False
    assert note  # non-empty honesty note
    assert "punt" in note.lower() or "could not" in note.lower()


def test_reconcile_unverified_success_claim_downgrades_to_false():
    ok, note = reconcile_ok(
        True,
        "I fixed the off-by-one bug and all tests pass.",
        tests_passed=False,
    )
    assert ok is False
    assert note
    assert "verif" in note.lower() or "not verified" in note.lower()


def test_reconcile_verified_success_claim_stays_ok_true():
    ok, note = reconcile_ok(
        True,
        "I fixed the off-by-one bug and all tests pass.",
        tests_passed=True,
    )
    assert ok is True
    assert note == ""


def test_reconcile_honest_non_claim_success_unchanged():
    ok, note = reconcile_ok(
        True,
        "Here is the square function you asked for.",
        tests_passed=False,
    )
    assert ok is True
    assert note == ""


def test_reconcile_punt_takes_precedence_over_success_claim():
    # An answer that both claims success AND punts is a punt → ok=False.
    ok, note = reconcile_ok(
        True,
        "I fixed it but the file is too large to verify, provide a smaller snippet.",
        tests_passed=False,
    )
    assert ok is False
    assert note


def test_reconcile_already_false_left_false():
    ok, note = reconcile_ok(
        False,
        "Here is the square function.",
        tests_passed=False,
    )
    assert ok is False
    assert note == ""


def test_reconcile_punt_when_ok_already_false_stays_false_no_double_note():
    ok, note = reconcile_ok(
        False,
        "I could not process the file, it is too large.",
        tests_passed=False,
    )
    assert ok is False
    # ok was already False; no downgrade was needed, so no note to append.
    assert note == ""
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_completion_guard.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.orchestrator.completion_guard'`

- [ ] **Step 3: Write the pure implementation**

Create `services/orchestrator/completion_guard.py`:

```python
# services/orchestrator/completion_guard.py
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_completion_guard.py -q`
Expected: PASS — all parametrized + reconcile cases green (≈ 30+ cases).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/completion_guard.py tests/services/orchestrator/test_completion_guard.py
git commit -m "feat(orchestrator): pure completion_guard (is_punt_answer, asserts_success, reconcile_ok)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Wire-in (a) — reconcile the single-skill result in `_run_skill_first`

The false-positive `ok=True` PUNT (report §4.5, skill_first c3) entered here: a read-only skill returned `ok=True` with a "file too large" message, and `_run_skill_first` finalized it verbatim. Apply `reconcile_ok` to the formatted `(ok, text)` right before the return. A single-skill goal has no in-run test verification, so `tests_passed=False` here — which is correct: the only thing this seam can fix is the PUNT shape (the success-claim downgrade for skills with no test run is acceptable and honest, but in practice skill answers rarely assert "tests pass", so the punt branch is the live fix).

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py` — top-of-file import; `_run_skill_first` return statement (the `return {"ok": ok, "summary": text[:2000], "tools_used": tools_list}` line near the end of `_run_skill_first`).
- Test: `tests/services/orchestrator/test_coding_orchestrator.py` (append).

**Interfaces:**
- Consumes: `reconcile_ok(ok, answer, *, tests_passed) -> (bool, str)` from Task 1.
- Produces: `_run_skill_first` still returns `{"ok": bool, "summary": str, "tools_used": list}` (shape unchanged); only `ok`/`summary` differ when the skill answer is a punt.

- [ ] **Step 1: Write the failing unit test**

Append to `tests/services/orchestrator/test_coding_orchestrator.py`:

```python
class TestSkillFirstPuntReconciliation:
    """Wire-in (a): a read-only skill that returns ok=True with a PUNT answer
    ('file too large') must be downgraded to ok=False by reconcile_ok."""

    @pytest.mark.asyncio
    async def test_skill_first_punt_answer_downgraded_to_ok_false(self):
        from services.orchestrator.coding_orchestrator import AsyncOrchestrator

        orch = AsyncOrchestrator(skill_router=MagicMock(), mcp=None, workspace="/tmp")
        # skill_router.run returns a ok=True result whose text is a punt.
        orch.skill_router.run = AsyncMock(return_value={
            "ok": True,
            "result": (
                "I couldn't analyze the file because it is too large. "
                "Please provide a smaller snippet."
            ),
            "skill_name": "repo-fault-localize",
        })

        out = await orch._run_skill_first("find the bug in /workspace/huge.py")

        assert out is not None
        assert out["ok"] is False
        assert "too large" in out["summary"].lower()
        assert "completion-guard" in out["summary"].lower()

    @pytest.mark.asyncio
    async def test_skill_first_honest_success_unchanged(self):
        from services.orchestrator.coding_orchestrator import AsyncOrchestrator

        orch = AsyncOrchestrator(skill_router=MagicMock(), mcp=None, workspace="/tmp")
        orch.skill_router.run = AsyncMock(return_value={
            "ok": True,
            "result": "Here are three potential bugs I found in the file.",
            "skill_name": "code-review",
        })

        out = await orch._run_skill_first("review /workspace/app.py for bugs")

        assert out is not None
        assert out["ok"] is True
        assert "completion-guard" not in out["summary"].lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest "tests/services/orchestrator/test_coding_orchestrator.py::TestSkillFirstPuntReconciliation" -q`
Expected: FAIL — `test_skill_first_punt_answer_downgraded_to_ok_false` asserts `ok is False` but current code returns `ok=True` (no reconciliation yet).

- [ ] **Step 3: Add the import**

In `services/orchestrator/coding_orchestrator.py`, in the import block (after the `from .verification_stop import needs_verification, build_verify_nudge` line near the top), add:

```python
from .completion_guard import reconcile_ok
```

- [ ] **Step 4: Apply `reconcile_ok` in `_run_skill_first`**

In `_run_skill_first`, replace the final return:

```python
        # Include the skill name in tools_used for curator sequence tracking
        skill_name = skill_result.get("skill_name", "") if isinstance(skill_result, dict) else ""
        tools_list = [skill_name] if skill_name else []
        return {"ok": ok, "summary": text[:2000], "tools_used": tools_list}
```

with:

```python
        # Include the skill name in tools_used for curator sequence tracking
        skill_name = skill_result.get("skill_name", "") if isinstance(skill_result, dict) else ""
        tools_list = [skill_name] if skill_name else []
        # Reconcile ok with the answer: a single-skill goal runs no in-loop test
        # verification, so tests_passed=False. The live fix here is the PUNT
        # shape — a read-only skill that returns ok=True with "file too large /
        # provide a snippet" must NOT be reported as a success (report §4.5).
        summary = text[:2000]
        recon_ok, note = reconcile_ok(ok, summary, tests_passed=False)
        if note:
            summary = (summary + " " + note)[:2000]
        return {"ok": recon_ok, "summary": summary, "tools_used": tools_list}
```

- [ ] **Step 5: Run the new tests + the existing `_run_skill_first` tests to confirm pass + no regression**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest "tests/services/orchestrator/test_coding_orchestrator.py::TestSkillFirstPuntReconciliation" tests/services/orchestrator/test_coding_orchestrator.py -q -k "skill_first or SkillFirst"`
Expected: PASS — new punt downgrade + honest-success-unchanged green; pre-existing skill-first tests still green.

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/coding_orchestrator.py tests/services/orchestrator/test_coding_orchestrator.py
git commit -m "fix(orchestrator): reconcile single-skill ok with punt answer in _run_skill_first

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Wire-in (b) — claim-gate the `finish` branch in `_run_react_loop`

The `finish` branch already has an honest-annotation path when `edited_files and not tests_passed`, but it still returns `ok=True` and does NOT catch a success CLAIM in a goal that edited nothing (or whose edit was indirect). Complement the existing verification-stop guard: after the nudge cap is exhausted (or no nudge was owed), run `reconcile_ok(ok=True, summary, tests_passed=tests_passed)` reusing the loop's existing `tests_passed` boolean. A punt summary → `ok=False`; an "I fixed it" claim with `tests_passed is False` → `ok=False` + caveat. A verified claim (`tests_passed True`) stays `ok=True`.

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py` — the `finish` branch of `_run_react_loop` (the block beginning `if name == "finish":` and ending with `return {"ok": True, "summary": summary, "tools_used": _tools_used}`).
- Test: `tests/services/orchestrator/test_coding_orchestrator.py` (append).

**Interfaces:**
- Consumes: `reconcile_ok` (already imported in Task 2); the loop-local `tests_passed: bool` and `edited_files: set[str]`.
- Produces: `_run_react_loop` finish return shape unchanged (`{"ok", "summary", "tools_used"}`); only `ok`/`summary` change for the punt + unverified-claim shapes.

- [ ] **Step 1: Write the failing unit test**

Append to `tests/services/orchestrator/test_coding_orchestrator.py`. This mirrors the helper style of `test_verification_stop_bdd.py` (a `_tool_call_msg`-style mock driving `litellm.acompletion`), kept local to the test:

```python
class TestReactFinishClaimGating:
    """Wire-in (b): the finish branch downgrades an unverified 'I fixed it'
    claim (no passing run_tests this run) to ok=False with a caveat, and
    downgrades a punt summary to ok=False."""

    @staticmethod
    def _finish_msg(summary: str):
        import json as _json
        tc = MagicMock()
        tc.id = "call-finish"
        tc.function = MagicMock()
        tc.function.name = "finish"
        tc.function.arguments = _json.dumps({"summary": summary})
        msg = MagicMock()
        msg.tool_calls = [tc]
        msg.content = ""
        msg.reasoning_content = ""
        msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
        return MagicMock(choices=[MagicMock(message=msg)])

    @pytest.mark.asyncio
    async def test_unverified_fix_claim_downgraded_with_caveat(self):
        from services.orchestrator.coding_orchestrator import AsyncOrchestrator

        orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
        # finish on turn 1 with a success claim; NO run_tests happened → tests_passed False.
        with patch(
            "services.orchestrator.coding_orchestrator.acompletion_with_failover",
            new_callable=AsyncMock,
            side_effect=[self._finish_msg("I fixed the off-by-one bug and all tests pass.")],
        ):
            out = await orch._run_react_loop("fix the factorial bug", 4)

        assert out["ok"] is False
        assert "completion-guard" in out["summary"].lower()

    @pytest.mark.asyncio
    async def test_punt_summary_downgraded(self):
        from services.orchestrator.coding_orchestrator import AsyncOrchestrator

        orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
        with patch(
            "services.orchestrator.coding_orchestrator.acompletion_with_failover",
            new_callable=AsyncMock,
            side_effect=[self._finish_msg(
                "I could not complete this; the file is too large, provide a smaller snippet."
            )],
        ):
            out = await orch._run_react_loop("fix the file", 4)

        assert out["ok"] is False
        assert "too large" in out["summary"].lower()

    @pytest.mark.asyncio
    async def test_honest_neutral_finish_stays_ok_true(self):
        from services.orchestrator.coding_orchestrator import AsyncOrchestrator

        orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
        with patch(
            "services.orchestrator.coding_orchestrator.acompletion_with_failover",
            new_callable=AsyncMock,
            side_effect=[self._finish_msg("Here is the square function you asked for.")],
        ):
            out = await orch._run_react_loop("write a square function", 4)

        assert out["ok"] is True
        assert "completion-guard" not in out["summary"].lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest "tests/services/orchestrator/test_coding_orchestrator.py::TestReactFinishClaimGating" -q`
Expected: FAIL — `test_unverified_fix_claim_downgraded_with_caveat` and `test_punt_summary_downgraded` assert `ok is False` but the current finish branch returns `ok=True`.

- [ ] **Step 3: Apply `reconcile_ok` in the `finish` branch**

In `_run_react_loop`, replace the tail of the `finish` branch:

```python
                        # Either no verification was owed, or the nudge cap was
                        # reached. If we edited without ever verifying, annotate
                        # the summary honestly rather than claiming a pass.
                        if edited_files and not tests_passed:
                            summary = (
                                summary + " [verification-stop: tests were NOT "
                                "verified to pass within the nudge budget]"
                            )[:2000]
                        return {"ok": True, "summary": summary, "tools_used": _tools_used}
```

with:

```python
                        # Either no verification was owed, or the nudge cap was
                        # reached. If we edited without ever verifying, annotate
                        # the summary honestly rather than claiming a pass.
                        if edited_files and not tests_passed:
                            summary = (
                                summary + " [verification-stop: tests were NOT "
                                "verified to pass within the nudge budget]"
                            )[:2000]
                        # Reconcile the final ok with the finish summary, reusing
                        # the verification-stop guard's tests_passed signal so a
                        # success CLAIM ("I fixed it / tests pass") that was NOT
                        # backed by a passing run_tests this run is gated, and a
                        # punt summary is never reported as a success (§4.5).
                        recon_ok, note = reconcile_ok(
                            True, summary, tests_passed=tests_passed
                        )
                        if note:
                            summary = (summary + " " + note)[:2000]
                        return {"ok": recon_ok, "summary": summary, "tools_used": _tools_used}
```

- [ ] **Step 4: Run the new tests + the full verification-stop suite to confirm pass + no regression**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest "tests/services/orchestrator/test_coding_orchestrator.py::TestReactFinishClaimGating" tests/services/orchestrator/test_verification_stop.py tests/services/orchestrator/test_verification_stop_bdd.py -q`
Expected: PASS — new claim-gating tests green; the verification-stop scenario "Edit then finish ... verifies and finishes" stays `ok=True` (its turn-3 `run_tests` passes → `tests_passed True`), proving the verified path is untouched.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/coding_orchestrator.py tests/services/orchestrator/test_coding_orchestrator.py
git commit -m "fix(orchestrator): claim-gate react finish via reconcile_ok (reuse tests_passed)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: BDD — punt → ok=False; unverified "I fixed it" → ok=False + caveat

End-to-end Gherkin coverage of both report failure shapes through the real `_run_react_loop`, mirroring the `@mocked` pattern and helpers of `test_verification_stop_bdd.py`.

**Files:**
- Create: `tests/services/orchestrator/features/ok_reconciliation.feature`
- Create: `tests/services/orchestrator/test_ok_reconciliation_bdd.py`

**Interfaces:**
- Consumes: `AsyncOrchestrator._run_react_loop` (production seam wired in Task 3); `run_async` from `tests/conftest.py`.
- Produces: nothing imported elsewhere.

- [ ] **Step 1: Write the failing feature file**

Create `tests/services/orchestrator/features/ok_reconciliation.feature`:

```gherkin
@mocked
Feature: OK/answer reconciliation (completion guard)
  The ok flag must agree with the final answer. A terminal punt ("file too
  large / provide a snippet") must never be ok=True, and an "I fixed it"
  success claim that was not backed by a passing test run this turn must be
  downgraded to ok=False with an honesty caveat. A neutral honest answer, and
  a success claim that WAS verified, are left ok=True unchanged.

  Background:
    Given a reconciliation AsyncOrchestrator with no skill router and no mcp

  Scenario: A punt finish is reported as not-ok
    Given the model calls finish with summary "I could not analyze the file because it is too large; provide a smaller snippet" on turn 1
    When the reconciliation loop runs the goal "find the bug in huge.py"
    Then the reconciled ok is False
    And the reconciled summary contains "too large"

  Scenario: An unverified fix claim is downgraded with a caveat
    Given the model calls finish with summary "I fixed the off-by-one bug and all tests pass" on turn 1
    When the reconciliation loop runs the goal "fix the factorial bug"
    Then the reconciled ok is False
    And the reconciled summary contains "completion-guard"

  Scenario: A neutral honest answer stays ok
    Given the model calls finish with summary "Here is the square function you asked for" on turn 1
    When the reconciliation loop runs the goal "write a square function"
    Then the reconciled ok is True
    And the reconciled summary does not contain "completion-guard"
```

- [ ] **Step 2: Write the step-def module**

Create `tests/services/orchestrator/test_ok_reconciliation_bdd.py`:

```python
# tests/services/orchestrator/test_ok_reconciliation_bdd.py
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.coding_orchestrator import AsyncOrchestrator
from tests.conftest import run_async

scenarios("features/ok_reconciliation.feature")


def _finish_msg(summary: str):
    tc = MagicMock()
    tc.id = "call-finish"
    tc.function = MagicMock()
    tc.function.name = "finish"
    tc.function.arguments = json.dumps({"summary": summary})
    msg = MagicMock()
    msg.tool_calls = [tc]
    msg.content = ""
    msg.reasoning_content = ""
    msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    return MagicMock(choices=[MagicMock(message=msg)])


@pytest.fixture
def ctx():
    return {"responses": [], "result": None}


@given("a reconciliation AsyncOrchestrator with no skill router and no mcp")
def _orch(ctx):
    ctx["orch"] = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")


@given(parsers.parse('the model calls finish with summary "{summary}" on turn {turn:d}'))
def _finish_on_turn(ctx, summary, turn):
    while len(ctx["responses"]) < turn:
        ctx["responses"].append(_finish_msg("filler"))
    ctx["responses"][turn - 1] = _finish_msg(summary)


@when(parsers.parse('the reconciliation loop runs the goal "{goal}"'))
def _run(ctx, goal):
    async def _go():
        with patch(
            "services.orchestrator.coding_orchestrator.acompletion_with_failover",
            new_callable=AsyncMock,
            side_effect=ctx["responses"],
        ):
            return await ctx["orch"]._run_react_loop(goal, 4)

    ctx["result"] = run_async(_go())


@then(parsers.parse("the reconciled ok is {value}"))
def _ok_is(ctx, value):
    assert ctx["result"]["ok"] is (value == "True")


@then(parsers.parse('the reconciled summary contains "{needle}"'))
def _summary_contains(ctx, needle):
    assert needle.lower() in ctx["result"]["summary"].lower()


@then(parsers.parse('the reconciled summary does not contain "{needle}"'))
def _summary_not_contains(ctx, needle):
    assert needle.lower() not in ctx["result"]["summary"].lower()
```

- [ ] **Step 3: Run the BDD scenarios to verify they pass**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_ok_reconciliation_bdd.py -q`
Expected: PASS — 3 scenarios green (punt→False+"too large"; unverified claim→False+"completion-guard"; neutral→True).

- [ ] **Step 4: Run the whole orchestrator suite (regression gate)**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/ -q`
Expected: PASS — all green; existing verification-stop, skill-first, and find-and-fix tests unaffected (additive change only).

- [ ] **Step 5: Commit**

```bash
git add tests/services/orchestrator/features/ok_reconciliation.feature tests/services/orchestrator/test_ok_reconciliation_bdd.py
git commit -m "test(orchestrator): BDD for ok/answer reconciliation (punt + unverified-claim)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage:**
- `is_punt_answer` / `asserts_success` / `reconcile_ok` pure + exhaustively unit-tested → Task 1 (punt phrases→True; neutral/successful→False; reconcile downgrades the two shapes; honest+verified left alone; punt-precedence; already-False no-note).
- Wire-in (a) `_run_skill_first` result path (the false-ok source) → Task 2.
- Wire-in (b) `_run_react_loop` finish + reuse of the verification-stop `tests_passed` signal → Task 3.
- Regression-safe (verified success stays ok=True) → Task 1 `test_reconcile_verified_success_claim_stays_ok_true`, Task 3 verification-stop suite re-run, Task 4 neutral scenario + whole-suite gate.
- BDD: punt→ok=False, unverified "I fixed it"→ok=False + caveat → Task 4 feature/steps.

**2. Placeholder scan:** No TBD/TODO/"handle edge cases"/"write tests for the above". Every code step shows complete code; every command has expected output. The two edits in `coding_orchestrator.py` are anchored on the verbatim surrounding lines (the `_run_skill_first` final return; the finish-branch tail), not line numbers — re-grep `return {"ok": ok, "summary": text\[:2000\], "tools_used": tools_list}` and `return {"ok": True, "summary": summary, "tools_used": _tools_used}` to locate them, since line numbers drift.

**3. Type consistency:** `reconcile_ok(ok, answer, *, tests_passed) -> tuple[bool, str]` defined in Task 1 and consumed identically in Tasks 2/3 (keyword-only `tests_passed`, returns `(ok, note)`, caller appends `note` to summary). `is_punt_answer`/`asserts_success` take `str`→`bool` throughout. Both wire-ins keep the existing return dict shape `{"ok", "summary", "tools_used"}`. The import line `from .completion_guard import reconcile_ok` is added once in Task 2 and relied on by Task 3.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-06-26-fix-ok-reconciliation.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — execute tasks in this session via executing-plans, batch with checkpoints.

**Which approach?**
