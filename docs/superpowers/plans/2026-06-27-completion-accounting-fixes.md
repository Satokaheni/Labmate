# Completion-Accounting Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the ReAct harness from scoring genuinely-verified work as `ok=False`. The A/B (`eval/reports/ab_findings_c1c2c3_2026-06-27.md`) showed the toolchain fix worked — tests run — but completion is credited only on a clean `finish`, "verified" is defined too narrowly, the edit-intent router mis-routes "write a test", and code-sandbox tool-name guessing burns turns. These are false-negative accounting bugs, not capability gaps.

**Architecture:** Four independent, additive fixes. (1) Credit a cut-off exit (budget/turn-cap/wall-clock/no-progress) as `ok=True` **only** when files were edited AND a real verification passed — never on a user cancel or an unverified run. (2) Count an assertion-bearing code-sandbox `run_python`/`run_shell` that exits 0 as verification (for tasks with no test suite). (3) Classify "write/add a test" as edit-intent so `skill_first` routes such goals into the ReAct loop. (4) Name code-sandbox's exact tools in the system prompt so the model stops guessing. The honesty guard stays intact: punts and unverified "I fixed it" claims remain `ok=False`; a *failing* test (the c3 "expose the bug" case) is never mis-credited.

**Tech Stack:** Python 3.11 (orchestrator), pytest + pytest-asyncio.

## Global Constraints

- Pure helpers (no I/O, no model calls) live in `completion_guard.py` / `edit_intent.py`, unit-testable without a model.
- Additive only: no existing public signature changes; new env knobs (none needed here) would default safe.
- The honesty contract is load-bearing: only credit on a cut-off exit when a *real* verification passed (`tests_passed=True`). Never credit a user cancel or an exception exit. Never treat a *failing* test as success.
- Byte-stable system prefix: the Fix #5 system-prompt addition must be a static string (no time/uuid/randomness) so llama.cpp prefix-cache reuse is preserved.
- Assert structure, not literal LLM text. `@pytest.mark.asyncio` on async tests.
- Honor CLAUDE.md rules (stdout-sacred in MCP servers; no tiktoken; Chroma client-server).

---

### Task 1: Credit verified work on cut-off exits (Fix #1)

The four non-`finish` loop exits return `ok=False` unconditionally and discard `tests_passed`. When the agent edited files AND a verification actually passed, a cut-off before `finish` should still be `ok=True` — the objective was met and the fix is on disk.

**Files:**
- Create helper in: `services/orchestrator/completion_guard.py`
- Modify: `services/orchestrator/coding_orchestrator.py` (exits at lines ~665, ~671, ~677, ~1151)
- Test: `tests/services/orchestrator/test_completion_guard.py`

**Interfaces:**
- Produces: `reconcile_cutoff(reason: str, *, edited_files: set[str], tests_passed: bool) -> tuple[bool, str]` — returns `(True, <completed note with reason>)` iff `edited_files and tests_passed`, else `(False, reason)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/services/orchestrator/test_completion_guard.py`:

```python
from services.orchestrator.completion_guard import reconcile_cutoff


def test_reconcile_cutoff_credits_verified_edit():
    ok, summary = reconcile_cutoff(
        "budget exhausted", edited_files={"a.py"}, tests_passed=True
    )
    assert ok is True
    assert "budget exhausted" in summary
    assert "tests passed" in summary.lower()


def test_reconcile_cutoff_no_edits_stays_false():
    ok, summary = reconcile_cutoff(
        "budget exhausted", edited_files=set(), tests_passed=True
    )
    assert ok is False
    assert summary == "budget exhausted"


def test_reconcile_cutoff_unverified_edit_stays_false():
    ok, summary = reconcile_cutoff(
        "wall-clock deadline exceeded", edited_files={"a.py"}, tests_passed=False
    )
    assert ok is False
    assert summary == "wall-clock deadline exceeded"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_completion_guard.py -k reconcile_cutoff -v`
Expected: FAIL — `reconcile_cutoff` does not exist.

- [ ] **Step 3: Implement the helper**

Add to `services/orchestrator/completion_guard.py`:

```python
def reconcile_cutoff(
    reason: str, *, edited_files: set[str], tests_passed: bool
) -> tuple[bool, str]:
    """Decide ok/summary for a loop exit that was NOT a clean finish.

    Credit ok=True ONLY when the agent edited files AND a real verification
    passed this run (tests_passed). Then the verification objective was met and
    the work is on disk even though the loop was cut off before an explicit
    finish. Otherwise preserve the original ok=False outcome and reason.
    Never call this for a user cancel or an exception exit.
    """
    if edited_files and tests_passed:
        return True, (
            f"Completed: edits were applied and tests passed; the loop stopped "
            f"on {reason} before an explicit finish."
        )
    return False, reason
```

- [ ] **Step 4: Wire it at the four cut-off exits**

In `services/orchestrator/coding_orchestrator.py`, import the helper (add to the existing `from .completion_guard import ...` line):

```python
from .completion_guard import reconcile_ok, reconcile_cutoff
```

Replace each of the four exits. Wall-clock (~665):

```python
                if deadline_s > 0 and (self._now() - start) > deadline_s:
                    _ok, _sum = reconcile_cutoff(
                        "wall-clock deadline exceeded",
                        edited_files=edited_files, tests_passed=tests_passed,
                    )
                    return {"ok": _ok, "summary": _sum, "tools_used": _tools_used}
```

Absolute turn limit (~671):

```python
                if not budget.record_turn():
                    _ok, _sum = reconcile_cutoff(
                        "absolute turn limit exceeded",
                        edited_files=edited_files, tests_passed=tests_passed,
                    )
                    return {"ok": _ok, "summary": _sum, "tools_used": _tools_used}
```

Budget exhausted (~677):

```python
                    if not budget.grace():
                        _ok, _sum = reconcile_cutoff(
                            "budget exhausted",
                            edited_files=edited_files, tests_passed=tests_passed,
                        )
                        return {"ok": _ok, "summary": _sum, "tools_used": _tools_used}
```

No-progress breaker (~1151):

```python
                if pstep.tripped:
                    _ok, _sum = reconcile_cutoff(
                        f"no-progress breaker tripped ({pstep.consecutive} consecutive idle turns)",
                        edited_files=edited_files, tests_passed=tests_passed,
                    )
                    return {"ok": _ok, "summary": _sum, "tools_used": _tools_used}
```

> DO NOT modify the user-cancel exit (~638, summary "cancelled by user mid-turn…") or the two exception exits (~1185, ~1385, summary "error: …"). Those must stay `ok=False`.

- [ ] **Step 5: Run the helper tests + the orchestrator loop tests**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_completion_guard.py -q`
Expected: PASS. Then run any existing loop/budget tests touching these exits:
Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator -k "budget or no_progress or wall or cutoff" -q`
Expected: PASS (update any test that asserted a hard `ok=False` on budget exhaustion *with* edits+passing tests — that case is now `ok=True` by design; a no-edit or unverified exhaustion stays `ok=False`).

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/completion_guard.py services/orchestrator/coding_orchestrator.py tests/services/orchestrator/test_completion_guard.py
git commit -m "fix(react): credit verified edits on cut-off exits (budget/turn/wall/no-progress)"
```

---

### Task 2: Count an assertion-backed code-sandbox run as verification (Fix #2)

`tests_passed` is set only by `run_tests`/pytest-via-`run_bash`. For a task with no test suite (e.g. c2), the model verifies by executing the function with an assertion via code-sandbox `run_python` — a correct verification that currently does not set `tests_passed`, so `reconcile_ok` downgrades the (correct) success. Credit it, but ONLY when the executed code actually asserts something and the run exits 0 (bare exit-0 means "didn't crash", not "correct").

**Files:**
- Create helper in: `services/orchestrator/completion_guard.py`
- Modify: `services/orchestrator/coding_orchestrator.py` (the `call_skill_tool` branch, ~909-917)
- Test: `tests/services/orchestrator/test_completion_guard.py`

**Interfaces:**
- Produces: `is_assertion_verification(skill: str, tool: str, arguments: dict, result: dict) -> bool` — True iff `skill == "code-sandbox"`, `tool in {"run_python","run_shell"}`, the submitted `code`/`cmd` contains an assertion (`assert`, `unittest`, `pytest`, `self.assert*`), AND the result envelope indicates a clean exit (exit_code 0 / no error).

- [ ] **Step 1: Write the failing tests**

Add to `tests/services/orchestrator/test_completion_guard.py`:

```python
from services.orchestrator.completion_guard import is_assertion_verification


def _envelope(exit_code=0, ok=True):
    return {
        "ok": ok,
        "result": {
            "content": [{"type": "text", "text":
                f'{{"stdout": "ok", "stderr": "", "exit_code": {exit_code}}}'}],
            "isError": exit_code != 0,
        },
    }


def test_assertion_run_python_pass_is_verification():
    assert is_assertion_verification(
        "code-sandbox", "run_python",
        {"code": "from ab_buggy import average\nassert average([2,4]) == 3"},
        _envelope(0),
    ) is True


def test_run_python_without_assert_is_not_verification():
    assert is_assertion_verification(
        "code-sandbox", "run_python",
        {"code": "print(average([2,4]))"},
        _envelope(0),
    ) is False


def test_assertion_run_python_nonzero_exit_is_not_verification():
    assert is_assertion_verification(
        "code-sandbox", "run_python",
        {"code": "assert average([2,4]) == 3"},
        _envelope(1, ok=False),
    ) is False


def test_other_skill_is_not_verification():
    assert is_assertion_verification(
        "test-gen", "generate", {"code": "assert True"}, _envelope(0)
    ) is False


def test_run_shell_with_assert_pass_is_verification():
    assert is_assertion_verification(
        "code-sandbox", "run_shell",
        {"cmd": "python -c 'assert 1==1'"},
        _envelope(0),
    ) is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_completion_guard.py -k assertion_verification -v`
Expected: FAIL — symbol missing.

- [ ] **Step 3: Implement the helper**

Add to `services/orchestrator/completion_guard.py` (add `import json`, `import re` at top if absent):

```python
_ASSERT_RE = re.compile(r"\bassert\b|\bunittest\b|\bpytest\b|self\.assert", re.IGNORECASE)


def _sandbox_exit_zero(result: dict) -> bool:
    """Extract a clean-exit signal from a skill_router code-sandbox envelope."""
    if not isinstance(result, dict) or not result.get("ok", False):
        return False
    inner = result.get("result")
    if isinstance(inner, dict):
        if inner.get("isError") is True:
            return False
        content = inner.get("content")
        if isinstance(content, list):
            for piece in content:
                text = piece.get("text") if isinstance(piece, dict) else None
                if not text:
                    continue
                try:
                    parsed = json.loads(text)
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(parsed, dict) and "exit_code" in parsed:
                    return int(parsed.get("exit_code") or 0) == 0
        # No structured exit_code but envelope ok and not isError -> treat as clean.
        return inner.get("isError") is not True
    return False


def is_assertion_verification(
    skill: str, tool: str, arguments: dict, result: dict
) -> bool:
    """True iff a code-sandbox run executed an assertion that PASSED.

    Used to set tests_passed for fix-without-a-test-suite tasks: the model runs
    the edited function with an `assert` via code-sandbox and it exits 0. A bare
    exit-0 (no assertion) is NOT verification — it only means the code didn't
    crash.
    """
    if skill != "code-sandbox" or tool not in ("run_python", "run_shell"):
        return False
    args = arguments or {}
    code = str(args.get("code") or args.get("cmd") or "")
    if not _ASSERT_RE.search(code):
        return False
    return _sandbox_exit_zero(result)
```

- [ ] **Step 4: Wire it into the `call_skill_tool` branch**

In `services/orchestrator/coding_orchestrator.py`, import the helper:

```python
from .completion_guard import reconcile_ok, reconcile_cutoff, is_assertion_verification
```

In the `elif name == "call_skill_tool" ...` branch (after `res = await self.skill_router.execute(...)` and `content = ground_tool_result(...)`, ~line 917), add:

```python
                        # Verification-stop: an assertion-bearing code-sandbox run
                        # that exits 0 is a real verification for tasks with no
                        # test suite (so a correct fix is not downgraded to ok=False).
                        if is_assertion_verification(
                            args.get("skill", ""), args.get("tool", ""),
                            args.get("arguments", {}), res,
                        ):
                            tests_passed = True
```

- [ ] **Step 5: Run the tests**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_completion_guard.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/completion_guard.py services/orchestrator/coding_orchestrator.py tests/services/orchestrator/test_completion_guard.py
git commit -m "fix(react): assertion-backed code-sandbox run counts as verification"
```

---

### Task 3: Classify "write/add a test" as edit-intent (Fix #3)

`requires_editing` is conservative and misses test-authoring, so `skill_first` sends "find the bug and write a unit test that exposes it" (c3) to a read-only skill that punts. Add a test-authoring rule so such goals enter the ReAct loop. Keep it narrow so pure "find bugs"/"review" goals stay read-only.

**Files:**
- Modify: `services/orchestrator/edit_intent.py`
- Test: `tests/services/orchestrator/test_edit_intent.py`

**Interfaces:**
- Produces: `classify_edit_intent` returns `requires_edit=True` with reason "test-authoring" for "write/add/create/author a (unit/integration) test(s)".

- [ ] **Step 1: Write the failing tests**

Add to `tests/services/orchestrator/test_edit_intent.py`:

```python
from services.orchestrator.edit_intent import requires_editing


def test_write_a_unit_test_is_edit_intent():
    assert requires_editing(
        "Find the bug in /workspace/ab_off.py and write a unit test that exposes it.",
        enabled=True,
    ) is True


def test_add_tests_is_edit_intent():
    assert requires_editing("Add tests for the parser module.", enabled=True) is True


def test_create_a_test_is_edit_intent():
    assert requires_editing("Create a test that reproduces the crash.", enabled=True) is True


def test_find_bugs_only_stays_read_only():
    # c4 control must NOT become edit-intent.
    assert requires_editing("Find bugs in /workspace/ab_buggy.py.", enabled=True) is False


def test_review_only_stays_read_only():
    assert requires_editing("Review the auth module for issues.", enabled=True) is False


def test_testing_word_does_not_falsely_trigger():
    # "testing" / "latest" must not match the test-authoring rule.
    assert requires_editing("Explain the latest testing strategy.", enabled=True) is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_edit_intent.py -k "test_or_review or write_a_unit or add_tests or create_a_test or find_bugs or testing_word" -v`
Expected: FAIL on the write/add/create cases (currently return False).

- [ ] **Step 3: Implement Rule 3**

In `services/orchestrator/edit_intent.py`, after the `_VERIFY_PHRASE_RE` definition, add:

```python
# Rule 3: test-authoring — "write a unit test", "add tests", "create a test".
# Narrow: an authoring verb immediately followed (optionally via a/an/some/new/
# unit/integration) by the word "test"/"tests". "testing"/"latest" do NOT match
# because \btests?\b requires a word boundary after "test".
_TEST_AUTHOR_RE = re.compile(
    r"\b(?:writ(?:e|es|ing)|add(?:s|ed|ing)?|creat(?:e|es|ing|ed)|author(?:s|ed|ing)?)\s+"
    r"(?:a\s+|an\s+|some\s+|new\s+|unit\s+|integration\s+)*tests?\b",
    re.IGNORECASE,
)
```

In `classify_edit_intent`, add the rule AFTER the verify-phrase check and BEFORE the final read/answer return:

```python
    if _VERIFY_PHRASE_RE.search(text):
        return EditIntent(requires_edit=True, reason="verification phrase (make tests pass / make it work)")

    if _TEST_AUTHOR_RE.search(text):
        return EditIntent(requires_edit=True, reason="test-authoring (write/add a test)")

    return EditIntent(
        requires_edit=False,
        reason="read/answer goal (no edit verb or verification phrase)",
    )
```

Update the module docstring's rule list to mention Rule 3.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_edit_intent.py -v`
Expected: PASS (new + existing).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/edit_intent.py tests/services/orchestrator/test_edit_intent.py
git commit -m "fix(routing): classify write/add-a-test goals as edit-intent"
```

---

### Task 4: Advertise code-sandbox tool names in the system prompt (Fix #5)

The model burns turns guessing code-sandbox tool names (`run_python_code`/`run_code`/`execute`) before hitting the real ones — and via Fix #1's exits, a wasted turn can be the difference between finishing and getting cut off. Name the exact tools once, up front, in the byte-stable system prefix.

**Files:**
- Modify: `services/orchestrator/prompt_assembler.py` (`BASE_SYSTEM_PROMPT`)
- Test: `tests/services/orchestrator/test_prompt_assembler.py`

**Interfaces:**
- Produces: `PromptAssembler(...).system_message()["content"]` contains the exact code-sandbox tool names.

- [ ] **Step 1: Write the failing test**

Add to `tests/services/orchestrator/test_prompt_assembler.py` (mirror the existing constructor usage in that file for how PromptAssembler is built):

```python
from services.orchestrator.prompt_assembler import BASE_SYSTEM_PROMPT


def test_base_prompt_names_code_sandbox_tools():
    for name in ("run_python", "run_shell", "run_tests", "install_packages"):
        assert name in BASE_SYSTEM_PROMPT
    # named together so the model uses them verbatim
    assert "code-sandbox" in BASE_SYSTEM_PROMPT
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_prompt_assembler.py -k code_sandbox_tools -v`
Expected: FAIL — names absent.

- [ ] **Step 3: Add the static, byte-stable line**

In `services/orchestrator/prompt_assembler.py`, append one sentence to the END of `BASE_SYSTEM_PROMPT` (keep it a plain string literal — no interpolation — so the prefix stays byte-stable):

```python
    "If you use the code-sandbox skill, its tools are EXACTLY: run_python, "
    "run_shell, run_tests, install_packages — call them with these names "
    "verbatim via call_skill_tool; do NOT guess other names."
```

(Concatenate it into the existing parenthesized string literal that defines `BASE_SYSTEM_PROMPT`, as the final segment.)

- [ ] **Step 4: Run the test; update any pinned-prompt test**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_prompt_assembler.py -v`
Expected: PASS. If a test pins the exact system-prompt string or a `canonical_prefix()` hash, update that expected value to the new text (the change is intentional). Re-run until green.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/prompt_assembler.py tests/services/orchestrator/test_prompt_assembler.py
git commit -m "fix(prompt): name code-sandbox tools up front to cut tool-name guessing"
```

---

### Task 5: Whole-suite regression gate

- [ ] **Step 1:** Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator -q` — Expected: PASS, no regressions.
- [ ] **Step 2:** Run: `PYTHONPATH=. python -m pytest tests/ -q 2>&1 | tail -5` — Expected: full suite green (live tests skipped), no new failures vs baseline.

---

## Self-Review

- **Spec coverage:** Fix #1 → Task 1; Fix #2 → Task 2; Fix #3 → Task 3; Fix #5 → Task 4. (Fix #4 / replan is a separate plan.) ✓
- **Honesty preserved:** Task 1 credits ONLY `edited_files and tests_passed`; cancel + exception exits untouched. Task 2 requires an actual assertion AND a clean exit (bare exit-0 rejected). A *failing* test never sets `tests_passed`, so c3 "expose the bug" is never mis-credited. Punts still hit `reconcile_ok` → `ok=False`. ✓
- **Type consistency:** `reconcile_cutoff` returns `(bool, str)` like `reconcile_ok`; `is_assertion_verification` consumes the skill_router envelope shape (`{"ok", "result": {"content":[{"text": json}]}}`) the loop already produces. ✓
- **Prefix stability:** Fix #5 adds a static literal segment; only the one-time prefix hash changes (pinned test updated), no per-call variation. ✓
- **No placeholders:** the only adapt-to-local note is the PromptAssembler constructor usage in Task 4 Step 1 (match the existing test file) and "update the pinned-prompt test if present" — both explicitly flagged. ✓
- **Live caveat:** these are accounting fixes; the real proof is the RunPod `TRIALS=3` A/B re-run. Expected: skill_first c1 → ~2/3 (Fix #1), c3 → ~3/3 (Fix #3), c2 holds; react c2 → passing (Fix #2); fewer wasted turns (Fix #5).
