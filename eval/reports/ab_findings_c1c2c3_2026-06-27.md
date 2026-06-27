# A/B Findings — why c1/c2/c3 failed (post-toolchain-fix, TRIALS=3)

**Date:** 2026-06-27  **Branch:** `feat/agentic-fix-loop`  **Model:** Gemma 4 31B Q4 (llama.cpp)
**Run:** `TRIALS=3` skill_first + react (+ replan c1-only). Evidence = per-trial Redis event streams
(`tool.done` results) joined to `tool.start` names, plus `coding_orchestrator.py` line refs.
**Scope:** diagnose skill_first c1/c3, react c2, the react-c1 1-of-3 split, react-c3-vs-skill_first-c3, replan c1.

> **Headline:** the toolchain fix worked — tests now *run*. The remaining failures are **not** the model
> being unable to do the work. In most "failing" compound trials the model **actually fixed the bug and the
> final `run_tests` passed**, but the harness **scored it `ok=False`**. The pass-rates **understate real
> success** because completion is only credited on a clean `finish`, and "verified" is defined too narrowly.

---

## Scored vs. actual (what really happened in each trial)

| case / mode | scored | **actual work** | gap |
|---|---|---|---|
| **skill_first c1** | 0/3 | T1 tests genuinely failed; **T2 & T3 fixed the bug, final `run_tests` exit 0 (PASS)** | T2/T3 are **false negatives** |
| **skill_first c3** | 0/3 | all 3: routed to one read-only skill (`repo-fault-localize`), punted "file too large", **0 edits, no test written** | mis-routing |
| **skill_first c2** | 2/3 | fixed `+1`/empty-list; verified via `code-sandbox run_python`; 2 trials finished cleanly | (the 1 fail = budget) |
| **react c1** | 1/3 | T1 never ran tests (genuine fail); **T2 PASS but `ok=False`**; T3 PASS and `ok=True` | T2 is a **false negative** |
| **react c2** | 0/3 | fixed the bug, verified via `code-sandbox run_python` (no test suite exists for c2); ran out of budget before a clean `finish` | **false negatives** |
| **react c3** | 3/3 | wrote a test that **fails on the buggy code (= exposes the bug)**, called `finish` | correct ✅ |
| **replan c1** | 0/3 | never reached `run_tests`; guessed wrong code-sandbox tool names; bailed "tools unavailable" | tool access |

Net: counting trials where the bug was actually fixed/exposed, true success was roughly **c1 ~4/6**,
**c2 ~? (≥2/3)**, **c3 3/3 (react)** — the scored numbers are depressed by accounting bugs, not capability.

---

## Root causes

### A. "Verified" is credited ONLY by `run_tests` / pytest-via-`run_bash` — not by `code-sandbox run_python`
`tests_passed` is set True in exactly two places: `run_tests` returning exit 0 (`coding_orchestrator.py:1038-1040`)
and a `run_bash` whose command contains `pytest` and passes (`:1007-1008`). A successful **`code-sandbox`
`run_python`** (the model executing the function and printing correct output) does **not** set it.

`reconcile_ok` then downgrades any success *claim* not backed by `tests_passed`
(`completion_guard.py:117`). So when a task has **no test suite** and the model verifies by running the
code directly, its (correct) "I fixed it" is gated to `ok=False`.

- **Hits react c2 hardest.** c2 = "review `ab_buggy.py`, then fix" — there is **no test file**. Every react c2
  trial verified via `code-sandbox run_python` (`{"ok": true, ... "stdout": "Average ..."}`) and **never
  called `run_tests`**. `tests_passed` stayed False the whole time.

### B. Completion is credited ONLY on a clean `finish`; every other exit hard-codes `ok=False` and discards `tests_passed`
The `finish` handler is the sole path that runs `reconcile_ok(True, summary, tests_passed=tests_passed)` and can
return `ok=True` (`:808-815`). **Every other loop exit returns `ok=False` unconditionally**, ignoring
`tests_passed`:

| exit | line | returns |
|---|---|---|
| budget exhausted (after grace) | **677** | `{"ok": False, "summary": "budget exhausted"}` |
| absolute turn limit | 671 | `{"ok": False, "summary": "absolute turn limit exceeded"}` |
| no-progress breaker | 1149 | `{"ok": False, ...}` |
| mid-turn cancel | 663 | `{"ok": False, ...}` |

So if the model reaches a **passing `run_tests` but the budget/turn cap cuts it off before it gets a turn to
call `finish`**, the run is scored `ok=False` even though `tests_passed=True` and the fix is on disk.

- **This is the skill_first c1 T2/T3 and react c1 T2 false negatives.** Trace proof — final tool result was
  `run_tests {"ok": true, "exit_code": 0, "raw_output": "... ........."}` and the answer was a genuine fix
  ("off-by-one … `range(1,n)` … fixed"), yet `ok=False`.

### C. The edit-intent router treats "write a test" as non-editing, so skill_first sends c3 to a read-only skill
With `ROUTE_EDIT_TO_REACT=1`, only goals classified `requires_editing` enter `_run_react_loop`; otherwise
skill_first runs a single deterministic skill (`_run_skill_first`, `:386`). c3 = "find the bug **and write a
unit test that exposes it**" is **not** classified as editing, so the selector picks `repo-fault-localize`
(a read-only locator), which returns a "file too large, send a snippet" punt → `reconcile_ok` correctly makes
it `ok=False`. The model **never enters the loop, never writes a test**. (Aside: `repo-fault-localize` calling
a 7-line file "too large" is its own bug.)

### D. replan's sub-goal path doesn't surface the working verification tools
In `replan` mode c1 there were **zero `run_tests` calls**; the model used `test-gen` then guessed wrong
code-sandbox tool names (`run_python_code`/`run_code` → `skill_unavailable`) and bailed "tools were
unavailable" in 13–36 calls / 25–75s. The flat `run_tests` tool and the code-sandbox SKILL.md tool-name
guidance that make react work are not reaching replan's per-sub-goal executor.

### (cross-cutting) code-sandbox tool-name guessing still burns turns
Even in react, the model repeatedly tries `run_python_code`/`run_code`/`execute`/`execute_python` before
hitting the real `run_python`/`run_tests`. The SKILL.md fix now returns a **helpful** error
("valid tools: install_packages, run_python, run_shell, run_tests"), but each wrong guess still costs a turn —
which, via root cause **B**, can be the difference between finishing and getting cut off.

---

## Direct answers to the five questions

1. **Why skill_first c1 still "fails" (0/3).** It mostly *doesn't*. T1 genuinely failed (tests still red). **T2
   and T3 fixed the bug and the final `run_tests` passed (exit 0), but were scored `ok=False`** because the
   budget/turn cap cut the loop off before a `finish`, and the budget-exhaustion exit (line 677) discards
   `tests_passed` (root cause **B**). True success ≈ 2/3.

2. **Why skill_first c3 fails (0/3).** Mis-routing (root cause **C**): c3 isn't classified as edit-intent, so
   skill_first runs one read-only skill (`repo-fault-localize`) that punts "file too large" — it never enters
   the loop or writes a test. 1 tool, 0 edits in every trial.

3. **Why react c2 fails (0/3).** Root cause **A** + **B**: c2 has no test suite, so the model verifies via
   `code-sandbox run_python` (which succeeds) — but only `run_tests` sets `tests_passed`, so the success claim
   is downgraded; and the extra code-sandbox tool-name churn pushed trials past the budget before a clean
   `finish` (T1's answer is literally "process was interrupted").

4. **Why react c1 passed 1 of 3 but failed 2.** All three did real work; the split is the **finish-vs-budget
   race** (root cause **B**): **T3** reached a passing `run_tests` *and got a turn to call `finish`* →
   credited `ok=True`. **T2** also reached a passing `run_tests` but the loop ended before `finish` →
   `ok=False` (false negative). **T1** never got tests to run at all (only failed code-sandbox calls) → a
   genuine incomplete. So "1/3" is really "2/3 did the work, 1 of those got credited."

5. **Why react passes c3 but skill_first fails c3.** Routing (root cause **C**). In `react` mode the loop is
   forced for every goal, so c3 writes a unit test, runs it, and the test **fails on the buggy `ab_off.py` —
   which is the success condition** (a failing test *exposes* the bug); the model calls `finish` with a
   bug-description answer (no "I fixed it" claim, so nothing is downgraded) → `ok=True` ×3. In `skill_first`
   mode the same goal is routed to the read-only `repo-fault-localize` skill (not edit-intent), which punts
   and never writes a test.

6. **Why c1 fails on replan.** Root cause **D**: replan's sub-goal executor never surfaces the working
   `run_tests` tool; the model guesses wrong code-sandbox tool names and gives up "tools unavailable" within
   13–36 calls. It doesn't reach the edit→run→verify path that react does.

---

## Recommended fixes (priority order)

1. **Credit `tests_passed` on non-`finish` exits.** At the budget-exhausted / turn-cap / no-progress returns
   (lines 677, 671, 1149), if `edited_files` and `tests_passed`, return `ok=True` (the verification objective
   was met) instead of a blanket `ok=False`. This alone likely flips skill_first c1 and react c1 to ~2/3 and
   stops discarding completed work. *(Highest impact, smallest change.)*
2. **Widen "verified" to include `code-sandbox run_python`/`run_shell` exit 0** (or a successful assertion run)
   so fix-without-a-test-suite tasks (c2) can set `tests_passed`. Fixes react c2.
3. **Classify "write/add a test" as `requires_editing`** so skill_first routes c3 into `_run_react_loop`
   instead of a read-only skill. (And fix `repo-fault-localize` mis-reporting a tiny file as "too large".)
4. **Give replan's sub-goal executor the same flat `run_tests` tool + code-sandbox SKILL.md guidance** the
   react loop has, so it can actually verify. Fixes replan c1.
5. **Reduce code-sandbox tool-name churn** — advertise the exact tool names in the system/tool prompt up front
   (not just on error), so wrong guesses don't burn the turns that root cause B punishes.

> Caveat: a *failing* `run_tests` is the **success** signal for c3-style "write a test that exposes the bug"
> tasks. Any change to test-outcome accounting must not assume "pass = good, fail = bad" universally — the
> objective (fix vs. expose) determines the desired exit code.
