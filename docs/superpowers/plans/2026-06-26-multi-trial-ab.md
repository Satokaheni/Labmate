# Multi-Trial A/B (Pass-Rate Scoring) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the sequencing A/B harness run each case N times and score on pass-rate instead of a single shot, so the Q4-model flakiness on c1/c3 (report §8 "Caveat") stops masking real deltas.

**Architecture:** Extract the pure result-shaping/aggregation logic out of `run_seq_ab.py`'s network loop into small importable functions (`aggregate_trials`, `median`, `summarize_case`). The Redis/orchestrator loop stays in the script body but now calls `run_case` `TRIALS` times per case (resetting the fixtures before *every* trial), then folds the per-trial dicts into one aggregated case record via the pure helper. A unit test exercises the pure helper with no Redis. `run_mode.sh` passes `TRIALS` through. No orchestrator/agent code is touched — this is eval-only.

**Tech Stack:** Python 3, `redis-py` (already a dep of the script), `pytest` (`asyncio_mode=auto`, but these tests are sync), `statistics` from the stdlib.

## Global Constraints

- **Eval-only — zero orchestrator risk.** Do NOT touch any file under `services/`. Only `eval/seq_ab/run_seq_ab.py`, `eval/seq_ab/run_mode.sh`, and a new test file change.
- **RunPod-only caveat preserved.** `run_mode.sh` hardcodes `/workspace/Labmate` and writes fixtures under `/workspace/`. Keep the existing RunPod-only comment and add one noting `TRIALS` is host-agnostic but the fixtures are still RunPod paths.
- **`TRIALS=1` reproduces today's behavior shape.** With `TRIALS=1` the harness must still produce a usable result; the per-case record is a documented *superset* of today's (it gains `trials: [...]` + aggregates while keeping `id`/`kind`/`task`/`ok`/`skill_sequence`/`llm_calls`/`wall_s` at the top level for older readers).
- **No LLM judge.** The harness only records the orchestrator's `ok` + skill sequence + numbers. Judging stays cross-family/manual (report §1, §8). Do NOT add any model call to this harness.
- **stdout note:** this is a standalone eval script, NOT an MCP server — `print(...)` is allowed and is the existing summary channel (the "stdout is sacred" rule applies only to MCP servers per CLAUDE.md §Critical Rules 1). Keep using `print(..., flush=True)`.
- **CLAUDE.md service URLs:** read `REDIS_URL` from env with the existing default `redis://localhost:6379/0`. Never hardcode.
- **File naming:** Python files `snake_case.py` (already satisfied).

---

## File Map

| File | Status | Responsibility |
|---|---|---|
| `eval/seq_ab/run_seq_ab.py` | Modify | Add `TRIALS` knob; add pure helpers `median`, `aggregate_trials`, `summarize_line`; loop `run_case` `TRIALS`× per case with per-trial fixture reset; write the richer per-case record; print per-case pass-rate summary. `run_case` itself is unchanged (already does one trial + one fixture reset). |
| `eval/seq_ab/run_mode.sh` | Modify | Pass `TRIALS` env through to the harness; extend the RunPod-only comment. |
| `tests/eval/seq_ab/test_aggregate_trials.py` | Create | Unit tests for the pure helpers (`median`, `aggregate_trials`, `summarize_line`) — no Redis, no orchestrator. |
| `tests/eval/__init__.py` | Create | Make `tests/eval` a package (mirrors `tests/services/` layout). |
| `tests/eval/seq_ab/__init__.py` | Create | Make `tests/eval/seq_ab` a package. |

**Key design facts (verified against the current files):**
- `run_seq_ab.py` is already import-safe: top level only does `import`s + module constants (`REDIS_URL`, `MODE`, `OUT`, `FIXTURES`, `CASES`) and defines functions; the Redis connection lives in `main()` under `if __name__ == "__main__":`. So `from eval.seq_ab.run_seq_ab import aggregate_trials` will NOT open Redis. **Constraint for the implementer:** keep it that way — the new pure helpers must not reference `redis`, `MODE`, or any module-level I/O.
- `MODE = sys.argv[1] if len(sys.argv) > 1 else "unknown"` runs at import. Under pytest `sys.argv[1]` is pytest's own arg, but `MODE` is never used by the pure helpers, so it is harmless. Do not depend on it in the helpers or tests.
- Today's per-case record (from `run_case`) has keys: `id, kind, task, ok, skill_sequence, llm_calls, wall_s, final_answer, task_id`. The aggregated record is a superset of these.

---

## New `results-<mode>.json` shape (target)

`TRIALS` per-case trial dicts are preserved verbatim under `trials`, plus aggregates. Top-level case keys mirror the FIRST trial so older readers that index `case["ok"]`/`case["skill_sequence"]`/`case["llm_calls"]`/`case["wall_s"]` keep working.

```json
{
  "mode": "skill_first",
  "trials": 3,
  "cases": [
    {
      "id": "c1_testgen_review_fix",
      "kind": "compound",
      "task": "Generate unit tests ...",
      "ok": true,
      "skill_sequence": ["test-gen", "code-sandbox"],
      "llm_calls": 20,
      "wall_s": 165.0,
      "final_answer": "...first trial's answer (<=1200 chars)...",
      "task_id": "ab-skill_first-c1_testgen_review_fix-1750000000",
      "pass_count": 2,
      "trials_run": 3,
      "pass_rate": 0.67,
      "median_llm_calls": 20,
      "median_wall_s": 165.0,
      "trials": [
        {"id": "c1_testgen_review_fix", "kind": "compound", "task": "...", "ok": true,
         "skill_sequence": ["test-gen","code-sandbox"], "llm_calls": 20, "wall_s": 165.0,
         "final_answer": "...", "task_id": "ab-skill_first-c1_...-1750000000"},
        {"...trial 2..."}, {"...trial 3..."}
      ]
    }
  ]
}
```

Notes:
- `pass_count` = number of trials with `ok is True`. `trials_run` = `len(trials)`. `pass_rate` = `round(pass_count / trials_run, 2)` (0.0 when `trials_run == 0`).
- `median_llm_calls` / `median_wall_s`: median over trials, ignoring `None` (a trial that timed out records `llm_calls=None`). If every value is `None`, the median is `None`.
- `trials_run` is the realized count (== `TRIALS` unless a future change skips a trial); `top-level` `trials` integer at the document root is the requested `TRIALS`.

### Helper signatures (the unit-testable core)

```python
def median(values: list[float | int | None]) -> float | int | None: ...
# median of the non-None values; None if all None/empty; numeric otherwise.

def aggregate_trials(trial_results: list[dict]) -> dict:
    # Returns {"pass_count", "trials_run", "pass_rate", "median_llm_calls",
    #          "median_wall_s", "trials": <the input list>}.
    # pass_rate = pass_count / trials_run, rounded to 2dp; 0.0 if trials_run == 0.

def summarize_line(mode: str, case_id: str, agg: dict) -> str:
    # One compact human line: "[mode] c1_...: 2/3 pass (rate=0.67) median_calls=20 median_wall=165.0s"
```

---

### Task 1: Pure aggregation helpers (`median`, `aggregate_trials`, `summarize_line`)

**Files:**
- Modify: `eval/seq_ab/run_seq_ab.py` (add three module-level functions; do not touch `run_case`/`main` yet)
- Create: `tests/eval/__init__.py`
- Create: `tests/eval/seq_ab/__init__.py`
- Test: `tests/eval/seq_ab/test_aggregate_trials.py`

**Interfaces:**
- Consumes: nothing (pure stdlib).
- Produces: `median(values)`, `aggregate_trials(trial_results)`, `summarize_line(mode, case_id, agg)` — imported by Task 2's `main`, and by the test. Trial dicts have the same keys `run_case` already returns (`ok`, `llm_calls`, `wall_s`, ...).

- [ ] **Step 1: Create the test package dirs and write the failing test**

Create `tests/eval/__init__.py` and `tests/eval/seq_ab/__init__.py` as empty files (package markers).

Create `tests/eval/seq_ab/test_aggregate_trials.py`:

```python
"""Unit tests for the pure aggregation helpers in eval/seq_ab/run_seq_ab.py.

These must run WITHOUT Redis or the orchestrator — the helpers are pure.
Importing run_seq_ab must not open a Redis connection (connection lives in main()).
"""
from eval.seq_ab.run_seq_ab import median, aggregate_trials, summarize_line


def _trial(ok, llm_calls, wall_s):
    # Mirrors the shape run_case() returns (only the fields aggregation reads).
    return {
        "id": "cX", "kind": "compound", "task": "t",
        "ok": ok, "skill_sequence": ["s"], "llm_calls": llm_calls,
        "wall_s": wall_s, "final_answer": "", "task_id": "tid",
    }


# ---- median ----
def test_median_odd():
    assert median([3, 1, 2]) == 2

def test_median_even_returns_mean_of_middle_two():
    assert median([1, 2, 3, 4]) == 2.5

def test_median_ignores_none():
    assert median([None, 4, 2, None]) == 3

def test_median_all_none_is_none():
    assert median([None, None]) is None

def test_median_empty_is_none():
    assert median([]) is None


# ---- aggregate_trials ----
def test_aggregate_all_pass():
    agg = aggregate_trials([_trial(True, 10, 5.0), _trial(True, 20, 7.0)])
    assert agg["pass_count"] == 2
    assert agg["trials_run"] == 2
    assert agg["pass_rate"] == 1.0
    assert agg["median_llm_calls"] == 15
    assert agg["median_wall_s"] == 6.0

def test_aggregate_all_fail():
    agg = aggregate_trials([_trial(False, 10, 5.0), _trial(False, 12, 6.0)])
    assert agg["pass_count"] == 0
    assert agg["pass_rate"] == 0.0
    assert agg["trials_run"] == 2

def test_aggregate_mixed_rounds_to_two_dp():
    agg = aggregate_trials([_trial(True, 1, 1.0), _trial(False, 2, 2.0), _trial(True, 3, 3.0)])
    assert agg["pass_count"] == 2
    assert agg["pass_rate"] == 0.67  # 2/3 -> 0.67

def test_aggregate_empty_no_div_by_zero():
    agg = aggregate_trials([])
    assert agg["pass_count"] == 0
    assert agg["trials_run"] == 0
    assert agg["pass_rate"] == 0.0
    assert agg["median_llm_calls"] is None
    assert agg["median_wall_s"] is None
    assert agg["trials"] == []

def test_aggregate_only_true_counts_as_pass():
    # ok can be None (orchestrator never answered) — must NOT count as a pass.
    agg = aggregate_trials([_trial(None, None, 30.0), _trial(True, 5, 5.0)])
    assert agg["pass_count"] == 1
    assert agg["pass_rate"] == 0.5

def test_aggregate_median_ignores_none_llm_calls():
    agg = aggregate_trials([_trial(None, None, 10.0), _trial(True, 8, 4.0), _trial(True, 12, 6.0)])
    assert agg["median_llm_calls"] == 10  # median of [8, 12]
    assert agg["median_wall_s"] == 6.0    # median of [10.0, 4.0, 6.0]

def test_aggregate_preserves_trials_list_identity_of_contents():
    trials = [_trial(True, 1, 1.0)]
    agg = aggregate_trials(trials)
    assert agg["trials"] == trials


# ---- summarize_line ----
def test_summarize_line_compact():
    agg = aggregate_trials([_trial(True, 10, 5.0), _trial(False, 20, 7.0), _trial(True, 30, 9.0)])
    line = summarize_line("skill_first", "c1_testgen_review_fix", agg)
    assert "skill_first" in line
    assert "c1_testgen_review_fix" in line
    assert "2/3" in line
    assert "0.67" in line
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/eval/seq_ab/test_aggregate_trials.py -q`
Expected: FAIL — collection/import error `ImportError: cannot import name 'median' from 'eval.seq_ab.run_seq_ab'` (helpers don't exist yet).

- [ ] **Step 3: Add the three pure helpers to `run_seq_ab.py`**

Insert these functions immediately after the `CASES = [...]` block (before `def reset_fixtures():`) so they sit at module scope and reference no Redis/`MODE` global:

```python
def median(values):
    """Median of the non-None numeric values; None if there are none."""
    nums = sorted(v for v in values if v is not None)
    n = len(nums)
    if n == 0:
        return None
    mid = n // 2
    if n % 2 == 1:
        return nums[mid]
    return (nums[mid - 1] + nums[mid]) / 2


def aggregate_trials(trial_results):
    """Fold a list of per-trial result dicts into one aggregate record.

    Pure: no Redis, no I/O. pass = trial['ok'] is True (None/False are not passes).
    """
    trials_run = len(trial_results)
    pass_count = sum(1 for t in trial_results if t.get("ok") is True)
    pass_rate = round(pass_count / trials_run, 2) if trials_run else 0.0
    return {
        "pass_count": pass_count,
        "trials_run": trials_run,
        "pass_rate": pass_rate,
        "median_llm_calls": median([t.get("llm_calls") for t in trial_results]),
        "median_wall_s": median([t.get("wall_s") for t in trial_results]),
        "trials": trial_results,
    }


def summarize_line(mode, case_id, agg):
    """One compact human-readable pass-rate line for a case."""
    return (
        f"[{mode}] {case_id}: {agg['pass_count']}/{agg['trials_run']} pass "
        f"(rate={agg['pass_rate']}) "
        f"median_calls={agg['median_llm_calls']} "
        f"median_wall={agg['median_wall_s']}s"
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/eval/seq_ab/test_aggregate_trials.py -q`
Expected: PASS — all 13 tests green.

- [ ] **Step 5: Confirm importing the script does NOT open Redis**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -c "import eval.seq_ab.run_seq_ab as m; print(hasattr(m, 'aggregate_trials'), hasattr(m, 'median'))"`
Expected: `True True` printed, process exits 0, NO Redis connection error (proves the helpers are import-safe and `main()` did not run).

- [ ] **Step 6: Commit**

```bash
cd /Users/zachstallbohm/Work/Labmate
git add eval/seq_ab/run_seq_ab.py tests/eval/__init__.py tests/eval/seq_ab/__init__.py tests/eval/seq_ab/test_aggregate_trials.py
git commit -m "test(eval): pure aggregation helpers for multi-trial A/B (pass-rate)"
```

---

### Task 2: Wire `TRIALS` into the case loop + richer output

**Files:**
- Modify: `eval/seq_ab/run_seq_ab.py` (add `TRIALS` env knob; loop `run_case` per trial; build aggregated case record; print pass-rate summary; add `trials` to the document root)

**Interfaces:**
- Consumes: `run_case(r, case)` (unchanged — already resets fixtures once + runs one trial), `aggregate_trials`, `summarize_line` (Task 1).
- Produces: the richer `results-<mode>.json` (see "New JSON shape" above) and per-case stdout summary lines.

- [ ] **Step 1: Add the `TRIALS` knob next to the other module constants**

In `run_seq_ab.py`, just below `OUT = f"eval/seq_ab/results-{MODE}.json"` (line ~13), add:

```python
TRIALS = int(os.getenv("TRIALS", "3"))  # run each case N times; pass-rate scoring. TRIALS=1 == single-shot.
```

- [ ] **Step 2: Rewrite `main()` to loop per trial and aggregate**

Replace the existing `main()` (currently lines ~87-98) with:

```python
def main():
    r = redis.from_url(REDIS_URL, decode_responses=True)
    out = {"mode": MODE, "trials": TRIALS, "cases": []}
    for case in CASES:
        print(f"[{MODE}] running {case['id']} x{TRIALS} ...", flush=True)
        trial_results = []
        for t in range(TRIALS):
            res = run_case(r, case)  # resets fixtures + runs one trial
            print(
                f"    trial {t + 1}/{TRIALS}: ok={res['ok']} seq={res['skill_sequence']} "
                f"calls={res['llm_calls']} {res['wall_s']}s",
                flush=True,
            )
            trial_results.append(res)
        agg = aggregate_trials(trial_results)
        # Case record: first trial's fields at top level (back-compat for single-shot
        # readers) + the aggregates + the full per-trial list.
        first = trial_results[0] if trial_results else {}
        case_record = {**first, **agg}
        out["cases"].append(case_record)
        print("  " + summarize_line(MODE, case["id"], agg), flush=True)
    os.makedirs("eval/seq_ab", exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(out, f, indent=2)
    # Final pass-rate roll-up across all cases (quick read).
    print(f"[{MODE}] pass-rate summary:", flush=True)
    for c in out["cases"]:
        print("  " + summarize_line(MODE, c["id"], c), flush=True)
    print(f"[{MODE}] wrote {OUT}", flush=True)
```

Note: `{**first, **agg}` puts `agg`'s keys (`pass_count`, `pass_rate`, `trials`, ...) last so they win; `agg["trials"]` (the list) deliberately overwrites nothing in `first` (no key collision — `first` has no `trials` key). The top-level `ok`/`skill_sequence`/`llm_calls`/`wall_s` come from `first` (trial 1), preserving the single-shot shape readers expect. `summarize_line` reads only `pass_count`/`trials_run`/`pass_rate`/`median_*`, all present in `agg`, so passing `c` (the merged record) in the roll-up works.

- [ ] **Step 3: Verify the script still imports and the helper wiring is consistent (no live run)**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -c "import ast; ast.parse(open('eval/seq_ab/run_seq_ab.py').read()); print('parse-ok')"`
Expected: `parse-ok`.

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. TRIALS=1 python -c "import eval.seq_ab.run_seq_ab as m; print('TRIALS=', m.TRIALS)"`
Expected: `TRIALS= 1` (env override works; default would be 3). No Redis error (main not invoked).

- [ ] **Step 4: Re-run the unit tests (regression — helpers unchanged, must stay green)**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/eval/seq_ab/test_aggregate_trials.py -q`
Expected: PASS — all 13 tests still green.

- [ ] **Step 5: Commit**

```bash
cd /Users/zachstallbohm/Work/Labmate
git add eval/seq_ab/run_seq_ab.py
git commit -m "feat(eval): multi-trial A/B case loop with pass-rate aggregation (TRIALS knob)"
```

---

### Task 3: Pass `TRIALS` through `run_mode.sh`; preserve RunPod-only caveat

**Files:**
- Modify: `eval/seq_ab/run_mode.sh`

**Interfaces:**
- Consumes: `TRIALS` from the caller's environment (optional; defaults inside `run_seq_ab.py` to 3).
- Produces: an exported `TRIALS` visible to the `python eval/seq_ab/run_seq_ab.py "$MODE"` child.

- [ ] **Step 1: Extend the header comment with the RunPod-only + TRIALS note**

In `run_mode.sh`, replace the top comment block (lines 2-3):

```bash
# Restart the orchestrator under a given SEQUENCING_MODE, then run the A/B harness.
# Usage: run_mode.sh <skill_first|react|replan>
```

with:

```bash
# Restart the orchestrator under a given SEQUENCING_MODE, then run the A/B harness.
# Usage: run_mode.sh <skill_first|react|replan>
# Multi-trial: set TRIALS in the environment to run each case N times and score on
#   pass-rate (default 3; TRIALS=1 == single-shot). Example: TRIALS=5 run_mode.sh react
# RunPod-only: this script hardcodes /workspace/Labmate and the fixtures live under
#   /workspace/ (see run_seq_ab.py FIXTURES). On another host, run run_seq_ab.py directly.
```

- [ ] **Step 2: Export `TRIALS` (with default) before launching the harness**

In `run_mode.sh`, after the existing `export SEQUENCING_MODE="$MODE"` line (line 11), add:

```bash
export TRIALS="${TRIALS:-3}"   # per-case trials; pass-rate scoring (RunPod-only fixtures)
```

And change the harness invocation line (line 30) from:

```bash
python eval/seq_ab/run_seq_ab.py "$MODE"
```

to:

```bash
echo "[$MODE] TRIALS=$TRIALS"
python eval/seq_ab/run_seq_ab.py "$MODE"
```

- [ ] **Step 3: Syntax-check the shell script**

Run: `cd /Users/zachstallbohm/Work/Labmate && bash -n eval/seq_ab/run_mode.sh && echo shell-ok`
Expected: `shell-ok` (no syntax errors).

- [ ] **Step 4: Verify the env default expands correctly (no orchestrator)**

Run: `cd /Users/zachstallbohm/Work/Labmate && TRIALS= bash -c 'export TRIALS="${TRIALS:-3}"; echo "default=$TRIALS"' && TRIALS=5 bash -c 'export TRIALS="${TRIALS:-3}"; echo "override=$TRIALS"'`
Expected:
```
default=3
override=5
```

- [ ] **Step 5: Commit**

```bash
cd /Users/zachstallbohm/Work/Labmate
git add eval/seq_ab/run_mode.sh
git commit -m "feat(eval): run_mode.sh passes TRIALS through; RunPod-only caveat noted"
```

---

## Behavior (Unit-test layer) — matching how `eval/` is tested

The `eval/` harness has **no BDD/Gherkin layer** (verified: no `.feature` files reference seq_ab; the orchestrator's pytest-bdd layer lives under `tests/services/orchestrator/features/` and is for the agent, not the eval harness). The harness is a thin Redis driver script with no existing tests. Per the prompt, a **focused unit-test layer for the new pure aggregation helper** is the right fit — not an invented BDD layer. That layer is Task 1's `tests/eval/seq_ab/test_aggregate_trials.py`. Its behavioral coverage:

| Behavior | Test |
|---|---|
| pass_rate = passes / trials | `test_aggregate_all_pass`, `test_aggregate_mixed_rounds_to_two_dp` |
| only `ok is True` counts (None/False do not) | `test_aggregate_only_true_counts_as_pass`, `test_aggregate_all_fail` |
| empty trial list → no ZeroDivision, rate 0.0, medians None | `test_aggregate_empty_no_div_by_zero` |
| median of odd / even / with-None / all-None / empty | the five `test_median_*` |
| medians ignore `None` (timed-out trials) | `test_aggregate_median_ignores_none_llm_calls` |
| per-trial list preserved on the record | `test_aggregate_preserves_trials_list_identity_of_contents` |
| compact summary line carries mode/case/X-of-N/rate | `test_summarize_line_compact` |

The Redis/orchestrator loop (`run_case`, `main`) is deliberately NOT unit-tested — it requires a live orchestrator + Redis (RunPod) and is exercised by the live A/B run in §"Live verification" below, exactly as the harness is exercised today.

---

## Live verification (RunPod-only — run after the unit tests pass)

This mirrors report §6's reproduce steps, now multi-trial. Requires the full stack up + model healthy (CLAUDE.md §"Live E2E").

```bash
# Single-shot back-compat check: TRIALS=1 must produce the old-shaped top-level fields.
TRIALS=1 bash eval/seq_ab/run_mode.sh skill_first
python -c "import json; d=json.load(open('eval/seq_ab/results-skill_first.json')); \
c=d['cases'][0]; print('top-level keys present:', all(k in c for k in ['ok','skill_sequence','llm_calls','wall_s'])); \
print('trials=', d['trials'], 'pass_rate=', c['pass_rate'], 'len(trials)=', len(c['trials']))"
# Expected: top-level keys present: True ; trials= 1 ; pass_rate in {0.0,1.0} ; len(trials)= 1

# Multi-trial run (the actual deliverable): 3 trials per case, pass-rate scored.
TRIALS=3 bash eval/seq_ab/run_mode.sh skill_first
TRIALS=3 bash eval/seq_ab/run_mode.sh react
TRIALS=3 bash eval/seq_ab/run_mode.sh replan
# Watch stdout for per-case lines like:
#   [skill_first] c1_testgen_review_fix: 2/3 pass (rate=0.67) median_calls=20 median_wall=165.0s
# → eval/seq_ab/results-{skill_first,react,replan}.json now carry trials:[...] + pass_rate.
```

After the A/B, the orchestrator is left running in the **last** mode (`replan`). Restart with `infrastructure/local/start.sh` to return to default `skill_first` (report §6 caveat — preserved).

---

## Self-Review

**1. Spec coverage:**
- Run each case `TRIALS` times, reset fixtures before *every* trial → Task 2 loops `run_case` (which itself resets fixtures at its top, line 53) once per trial. ✅
- Record per-trial list + aggregates (`pass_count`/`trials_run`/`pass_rate`/median calls/median wall) → `aggregate_trials` (Task 1) + merged case record (Task 2). ✅
- Single-trial behavior reachable (`TRIALS=1`) without regression → `TRIALS=int(os.getenv("TRIALS","3"))`; merged record keeps first-trial top-level fields; live check verifies. ✅
- Write richer structure to `results-<mode>.json` + print compact per-case pass-rate summary → Task 2 `json.dump` + `summarize_line` per case and final roll-up. ✅
- `ok` interpretation unchanged; no LLM judge added → `aggregate_trials` only counts `ok is True`; no model call anywhere. ✅ (Global Constraints)
- Pure aggregation logic importable + unit-tested without Redis → helpers at module scope, no Redis refs; Step 5 of Task 1 proves import doesn't open Redis. ✅
- `run_mode.sh` passes `TRIALS` through + RunPod-only comment → Task 3. ✅
- Aggregation edge cases (pass_rate, median, empty/all-fail/all-pass) → Task 1 tests. ✅
- No orchestrator code touched → File Map only lists `eval/` + `tests/eval/`. ✅ (Global Constraints)

**2. Placeholder scan:** No TBD/TODO/"add error handling"/"similar to Task N"/bare test references. All code blocks are complete and runnable. ✅

**3. Type consistency:** `median`, `aggregate_trials`, `summarize_line` signatures and dict keys (`pass_count`, `trials_run`, `pass_rate`, `median_llm_calls`, `median_wall_s`, `trials`) are identical across the helper definitions (Task 1), the tests (Task 1), the `main()` wiring (Task 2), and the JSON-shape section. `run_case`'s returned keys (`ok`, `llm_calls`, `wall_s`, `skill_sequence`, ...) match what `aggregate_trials` reads. ✅
