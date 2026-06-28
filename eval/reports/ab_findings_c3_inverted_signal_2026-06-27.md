# A/B Findings — c3 inverted-signal fix (2026-06-27)

**Branch:** `feat/agentic-fix-loop`  **Host:** RunPod RTX 6000 Ada, llama.cpp Q4.
**Result:** with the c3 fix, **both modes are 15/15 (every case 3/3).**

| case | skill_first | react | before this session |
|---|---|---|---|
| c1 testgen→review→fix | 3/3 | 3/3 | 0/3 |
| c2 review→fix | 3/3 | 3/3 | 1/3 |
| c3 bug→test | **3/3** | **3/3** | 2/3 (this run's starting point) |
| c4 single review | 3/3 | 3/3 | 3/3 |
| c5 trivial | 3/3 | 3/3 | 3/3 |

This run validates the third fix of the session. The first two
(unwrap-readback + tests_passed-threading, commits `4b94bf1`/`8dd34c4`) took
c1 `0→3/3` and c2 `1→3/3`. This one closes c3.

## What caused the c3 failures

c3 = *"Find the bug … and write a unit test that **exposes** it."* For this task a
**failing** `run_tests` is the SUCCESS signal — the test reproduces the bug. The
harness assumed the opposite (pass = good) in two places, and that assumption only
became *active* once the earlier unwrap fix made `write_file` work and populate
`edited_files`:

1. **Verify-stop nudge steered the wrong way.** After the agent wrote the test
   (`edited_files` non-empty) and ran it to a FAIL (`tests_passed` still False),
   `needs_verification` fired and injected `build_verify_nudge` — *"run the tests …
   fix the code … only finish once they pass."* For c3 that is backwards. In react
   c3 trial 2 of the prior run (`ab-react-c3_bug_then_test-1782596950`) it drove the
   model into a `loop.detected` (repeat `run_tests`) → invalid `call_skill_tool`
   guesses → an **honest give-up**: *"I was unable to identify the bug."* (`ok=False`).

2. **Cut-off credit only counted a passing run.** `reconcile_cutoff` credits
   `edited_files AND tests_passed`. c3 legitimately never sets `tests_passed=True`,
   so a trial that wrote the exposing test but hit the iteration cap scored
   `ok=False` (skill_first `…-1782596352`: wrote a correct test, honest answer, but
   no clean finish → uncredited).

> Note: pre-unwrap-fix, c3 passed 3/3 partly **by accident** — writes went through
> code-sandbox heredocs, `edited_files` stayed empty, so the nudge never fired.
> Fixing `write_file` exposed the latent inversion. Net-correct change, new gap.

## The fix (commit `e3ea18d`)

A narrow `exposes_bug_intent()` predicate (`edit_intent.py`): matches *"test that
exposes/reproduces/demonstrates the bug"*, *"failing test"*, etc., and is
**disqualified** by any *"make/until … tests pass"* intent so a normal fix goal
(c1/c2) is never inverted (a misfire would let a fix goal finish red). When set:

- a test that **ran and FAILED** sets `tests_passed` (the unified
  verification-met signal) — so the existing nudge/finish/cut-off/threading paths
  all credit it with **no further change**; and
- the nudge is swapped for `build_expose_test_nudge()`, which steers toward
  RUNNING the test and confirming it **fails** (never toward making it pass).

Fix-goal semantics are unchanged (everything is gated on `exposes_bug_intent`).
Unit suite: orchestrator **1194 passed** (+21 new).

## Trace confirming the mechanism (react c3, `…-1782599389`, ok=True)

```
write_file  test_expose_bug.py  -> verified:true
run_tests   test_expose_bug.py  -> exit=1  (FAILING = bug exposed)   ← credited as success
RESULT ok=True   13 llm_calls   50s
final_answer: "...The test fails as expected (expecting index 3 but receiving
   index 1), confirming that the function does not correctly identify the last
   occurrence."   ← honest; no nudge thrash; no "make it pass"
```

Contrast the prior failure (22 calls, verify.nudge → loop.detected → give-up).

## Status

Fabrication eliminated; all three compound cases (c1/c2/c3) and both controls are
3/3 in both `skill_first` and `react`. No regressions. The session's three
completion-accounting fixes are committed and pushed.
