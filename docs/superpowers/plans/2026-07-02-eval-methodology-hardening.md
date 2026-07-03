# Eval-Methodology Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every number Labmate's routing and seq_ab evals report mean what it claims — labelled proxy-vs-goal, measured with variance, captured under a controlled single-axis comparison with recorded provenance, scored against a held-out non-model-generated slice and a trivial baseline.

**Architecture:** All work is **eval tooling only** — new pure helper modules under `eval/` with unit tests under `tests/eval/`, plus thin wiring into the two existing runners (`eval/run_routing_eval.py`, `eval/seq_ab/run_seq_ab.py`), two policy/protocol docs, and one held-out data file. No `services/orchestrator/` behavior changes. Pure functions carry the logic (unit-testable with no model/Redis/GPU); the runners only call them and print/stamp.

**Tech Stack:** Python 3, `pytest` + `pytest-asyncio`, stdlib only for the new math (no scipy — Wilson interval is closed-form). Follows the repo's existing eval patterns (`tests/eval/seq_ab/test_aggregate_trials.py` is the model to copy).

## Global Constraints

- **Measure first — no new harness mechanism.** These tools *measure* existing flags; they add no retry/guard/routing behavior. Goal-level retry already exists (`MAX_GOAL_ATTEMPTS`) — do not add another.
- **No harness-behavior code.** Touch `eval/`, `tests/eval/`, `docs/`, and `CLAUDE.md` only. Never `services/orchestrator/`.
- **Never modify `eval/routing_eval.seed.jsonl`** (locked) and never feed the held-out slice to `extend_eval.py`.
- **Cross-family grading only** — any result-file judgement uses Claude, never Gemma/Qwen.
- **stdout discipline** does not apply here (these are standalone scripts, not MCP servers) — `print` is fine in `eval/`.
- **Assert structure, not literal LLM text** (LLM output is non-deterministic); the new pure helpers are deterministic and may be asserted exactly.
- **Commits** end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **Never `git add -A`** — stage files by exact path. Never stage `services/frontend/src/config.ts` or `.codegraph/daemon.pid`.
- Run tests from the repo root: `python -m pytest <path> -v` (imports like `from eval.seq_ab.variance import ...` resolve from root).

## File Structure

**New (pure helpers + tests):**
- `eval/metric_meaning.py` — proxy-vs-goal caption strings + line builders (WS1)
- `eval/seq_ab/variance.py` — Wilson interval + interval-overlap test (WS2)
- `eval/seq_ab/baselines.py` — no-op floor per case kind (WS5, needed by compare)
- `eval/seq_ab/compare.py` — paired run comparison + win verdict (WS2)
- `eval/provenance.py` — provenance header build/capture/compare (WS3)
- `eval/routing_metrics.py` — majority/random baselines + leakage inflation (WS4+WS5)
- `eval/routing_eval.heldout.jsonl` — held-out, non-model-generated routing slice (WS4)
- `eval/seq_ab/run_flag_ab.sh` — live one-flag A/B wrapper (WS2, RunPod-only, smoke-tested)
- `docs/superpowers/eval-variance-policy.md` — the "when is a default a win" rule (WS2)
- `docs/superpowers/eval-controlled-comparison-protocol.md` — frozen-axis protocol (WS3)
- Tests: `tests/eval/test_metric_meaning.py`, `tests/eval/seq_ab/test_variance.py`, `tests/eval/seq_ab/test_baselines.py`, `tests/eval/seq_ab/test_compare.py`, `tests/eval/test_provenance.py`, `tests/eval/test_routing_metrics.py`, `tests/eval/test_heldout_slice.py`

**Modified (thin wiring):**
- `eval/seq_ab/run_seq_ab.py` — CI fields in `aggregate_trials`; provenance stamp in `main`; metric-meaning in output; long-tail cases/fixtures
- `eval/run_routing_eval.py` — metric header + baselines + `--heldout` scoring + inflation in `write_reports`/`main`; long-tail cases
- `eval/seq_ab/results-skill_first.json`, `results-react.json` → moved to `*.31b.ref.json` (WS3)
- `CLAUDE.md` — eval-section pointers to the new tooling (final task)

---

## Task 1: Proxy-vs-goal metric labelling (WS1)

**Files:**
- Create: `eval/metric_meaning.py`
- Test: `tests/eval/test_metric_meaning.py`

**Interfaces:**
- Produces: `ROUTING_MEANING: str`, `SEQ_AB_MEANING: str`, `routing_header_lines() -> list[str]`, `seq_ab_meaning_block() -> dict`

- [ ] **Step 1: Write the failing test**

```python
# tests/eval/test_metric_meaning.py
from eval.metric_meaning import (
    ROUTING_MEANING, SEQ_AB_MEANING, routing_header_lines, seq_ab_meaning_block,
)


def test_routing_meaning_names_the_proxy():
    text = ROUTING_MEANING.lower()
    assert "proxy" in text
    assert "skill selection" in text
    assert "not" in text and "task completion" in text


def test_routing_header_lines_are_markdown():
    lines = routing_header_lines()
    assert any(l.startswith("> ") for l in lines)  # blockquote caption
    assert any("proxy" in l.lower() for l in lines)


def test_seq_ab_meaning_flags_self_report():
    block = seq_ab_meaning_block()
    assert block["ok_metric"] == "proxy"
    assert "self-report" in block["note"].lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/eval/test_metric_meaning.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'eval.metric_meaning'`

- [ ] **Step 3: Write minimal implementation**

```python
# eval/metric_meaning.py
"""Proxy-vs-goal captions for eval reports.

Every gated metric is labelled here so a report reader can tell, without external
context, which numbers are PROXIES (a stand-in) and which are the GOAL (task done +
honest). No metric changes live here — only honest labelling.
"""

ROUTING_MEANING = (
    "Routing accuracy is a PROXY: it measures correct skill SELECTION (top-1), "
    "NOT task completion. A case can route correctly and the skill can then fail "
    "the task — this metric still counts it correct. The 0.80 bar is a review "
    "policy, not a code gate."
)

SEQ_AB_MEANING = (
    "seq_ab `ok` is a PROXY for task completion: it is the harness's OWN "
    "reconcile_ok() verdict (self-reported), not an independent judgement. "
    "`honesty` is assessed offline by a cross-family judge (Claude), not by this "
    "harness. The GOAL is: task actually completed AND no unearned success claim."
)


def routing_header_lines() -> list[str]:
    """Markdown blockquote lines to prepend to the routing report."""
    return [f"> {line}" for line in ROUTING_MEANING.split(". ") if line] + [""]


def seq_ab_meaning_block() -> dict:
    """Machine-readable meaning block for the seq_ab results JSON."""
    return {
        "ok_metric": "proxy",
        "goal": "task completed AND honest (no unearned success claim)",
        "note": (
            "`ok` is harness self-report via reconcile_ok(); honesty is an offline "
            "cross-family (Claude) judgement, not computed here."
        ),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/eval/test_metric_meaning.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add eval/metric_meaning.py tests/eval/test_metric_meaning.py
git commit -m "feat(eval): proxy-vs-goal metric captions (WS1)"
```

---

## Task 2: Wire metric captions into both runners (WS1)

**Files:**
- Modify: `eval/run_routing_eval.py` (`write_reports`, ~397-433)
- Modify: `eval/seq_ab/run_seq_ab.py` (`main`, the `out = {...}` dict ~146)
- Test: `tests/eval/test_metric_meaning.py` (add wiring assertions)

**Interfaces:**
- Consumes: `routing_header_lines`, `seq_ab_meaning_block` from Task 1.

- [ ] **Step 1: Add the failing wiring test**

```python
# append to tests/eval/test_metric_meaning.py
from pathlib import Path
from eval.run_routing_eval import write_reports


def test_routing_report_starts_with_meaning(tmp_path):
    summary = {
        "overall": 0.9, "n": 10, "mean_stability": 1.0, "by_cluster": {},
        "by_skill": {}, "by_kind": {}, "false_positive_rate": None, "confusion": [],
    }
    md = write_reports([], summary, str(tmp_path), repeats=3)
    body = Path(md).read_text().lower()
    assert "proxy" in body
    assert body.index("proxy") < body.index("overall accuracy")
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/eval/test_metric_meaning.py::test_routing_report_starts_with_meaning -v`
Expected: FAIL (`proxy` not found / assertion error)

- [ ] **Step 3: Implement — prepend caption in `write_reports`**

In `eval/run_routing_eval.py`, add the import near the top (after the existing imports):

```python
from eval.metric_meaning import routing_header_lines
```

Change the `lines = [ ... ]` initializer in `write_reports` (currently starting `f"# Routing eval — {stamp}",`) so the caption comes right after the title:

```python
    lines = [
        f"# Routing eval — {stamp}",
        "",
        *routing_header_lines(),
        f"- cases: {summary['n']}  |  repeats: {repeats}",
        f"- overall accuracy: {summary['overall']:.3f}",
        f"- mean stability: {summary['mean_stability']:.3f}",
        f"- false-positive rate (negatives): " f"{summary['false_positive_rate']:.3f}"
        if summary["false_positive_rate"] is not None
        else "- false-positive rate: n/a",
        "",
    ]
```

In `eval/seq_ab/run_seq_ab.py`, add the import at the top:

```python
from eval.metric_meaning import seq_ab_meaning_block
```

Change the output dict in `main` from `out = {"mode": MODE, "trials": TRIALS, "cases": []}` to:

```python
    out = {"mode": MODE, "trials": TRIALS, "metric_meaning": seq_ab_meaning_block(), "cases": []}
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/eval/test_metric_meaning.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add eval/run_routing_eval.py eval/seq_ab/run_seq_ab.py tests/eval/test_metric_meaning.py
git commit -m "feat(eval): surface proxy-vs-goal captions in both reports (WS1)"
```

---

## Task 3: Wilson interval + interval overlap (WS2)

**Files:**
- Create: `eval/seq_ab/variance.py`
- Test: `tests/eval/seq_ab/test_variance.py`

**Interfaces:**
- Produces: `wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]`, `intervals_disjoint(a: tuple[float, float], b: tuple[float, float]) -> bool`

- [ ] **Step 1: Write the failing test**

```python
# tests/eval/seq_ab/test_variance.py
import pytest
from eval.seq_ab.variance import wilson_interval, intervals_disjoint


def test_wilson_zero_n_is_full_uncertainty():
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_wilson_all_pass_is_high_and_bounded():
    low, high = wilson_interval(3, 3)
    assert 0.0 < low < 1.0
    assert high == pytest.approx(1.0, abs=1e-9) or high < 1.0
    assert low < 1.0  # 3/3 on n=3 is NOT certainty


def test_wilson_half_is_centered_below_point():
    low, high = wilson_interval(5, 10)
    assert low < 0.5 < high


def test_intervals_disjoint():
    assert intervals_disjoint((0.0, 0.3), (0.4, 0.9)) is True
    assert intervals_disjoint((0.4, 0.9), (0.0, 0.3)) is True
    assert intervals_disjoint((0.0, 0.5), (0.4, 0.9)) is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/eval/seq_ab/test_variance.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'eval.seq_ab.variance'`)

- [ ] **Step 3: Write minimal implementation**

```python
# eval/seq_ab/variance.py
"""Closed-form Wilson score interval for a binomial pass-rate, and a disjoint test.

Wilson is used (not normal-approx) because n is tiny (TRIALS>=3): it stays inside
[0,1] and never collapses to a zero-width interval at 0/n or n/n.
"""
import math


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95%-by-default Wilson score interval for `successes` out of `n`.

    n == 0 -> (0.0, 1.0) (no information). Result is clamped to [0, 1].
    """
    if n <= 0:
        return (0.0, 1.0)
    p = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    margin = (z * math.sqrt((p * (1 - p) + z2 / (4 * n)) / n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def intervals_disjoint(a: tuple[float, float], b: tuple[float, float]) -> bool:
    """True iff intervals a and b do not overlap (touching endpoints count as overlap)."""
    return a[1] < b[0] or b[1] < a[0]
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/eval/seq_ab/test_variance.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add eval/seq_ab/variance.py tests/eval/seq_ab/test_variance.py
git commit -m "feat(eval): Wilson pass-rate interval + disjoint test (WS2)"
```

---

## Task 4: Add CI fields to `aggregate_trials` (WS2)

**Files:**
- Modify: `eval/seq_ab/run_seq_ab.py` (`aggregate_trials`, ~61-76)
- Test: `tests/eval/seq_ab/test_aggregate_trials.py` (extend existing)

**Interfaces:**
- Consumes: `wilson_interval` (Task 3).
- Produces: `aggregate_trials` output gains `pass_rate_ci_low`, `pass_rate_ci_high`.

- [ ] **Step 1: Add the failing test**

```python
# append to tests/eval/seq_ab/test_aggregate_trials.py
from eval.seq_ab.run_seq_ab import aggregate_trials


def test_aggregate_includes_wilson_ci():
    agg = aggregate_trials([{"ok": True}, {"ok": True}, {"ok": False}])
    assert agg["pass_count"] == 2 and agg["trials_run"] == 3
    assert 0.0 <= agg["pass_rate_ci_low"] < agg["pass_rate"] < agg["pass_rate_ci_high"] <= 1.0


def test_aggregate_ci_empty_is_full_range():
    agg = aggregate_trials([])
    assert agg["pass_rate_ci_low"] == 0.0
    assert agg["pass_rate_ci_high"] == 1.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/eval/seq_ab/test_aggregate_trials.py -k ci -v`
Expected: FAIL (`KeyError: 'pass_rate_ci_low'`)

- [ ] **Step 3: Implement**

In `eval/seq_ab/run_seq_ab.py`, add the import at the top:

```python
from eval.seq_ab.variance import wilson_interval
```

Extend the returned dict in `aggregate_trials` (add two keys before `"trials"`):

```python
    ci_low, ci_high = wilson_interval(pass_count, trials_run)
    return {
        "pass_count": pass_count,
        "trials_run": trials_run,
        "pass_rate": pass_rate,
        "pass_rate_ci_low": round(ci_low, 3),
        "pass_rate_ci_high": round(ci_high, 3),
        "median_llm_calls": median([t.get("llm_calls") for t in trial_results]),
        "median_wall_s": median([t.get("wall_s") for t in trial_results]),
        "trials": trial_results,
    }
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/eval/seq_ab/test_aggregate_trials.py -v`
Expected: PASS (all, including the two new)

- [ ] **Step 5: Commit**

```bash
git add eval/seq_ab/run_seq_ab.py tests/eval/seq_ab/test_aggregate_trials.py
git commit -m "feat(eval): report Wilson CI on every case pass-rate (WS2)"
```

---

## Task 5: No-op trivial baseline for seq_ab (WS5, needed by compare)

**Files:**
- Create: `eval/seq_ab/baselines.py`
- Test: `tests/eval/seq_ab/test_baselines.py`

**Interfaces:**
- Produces: `noop_floor(kind: str) -> float`, `NOOP_PASS_BY_KIND: dict[str, float]`

- [ ] **Step 1: Write the failing test**

```python
# tests/eval/seq_ab/test_baselines.py
from eval.seq_ab.baselines import noop_floor


def test_noop_cannot_complete_any_case():
    # A do-nothing agent that finishes with no work passes nothing.
    assert noop_floor("compound") == 0.0
    assert noop_floor("control_single") == 0.0
    assert noop_floor("control_trivial") == 0.0


def test_noop_unknown_kind_defaults_zero():
    assert noop_floor("something_new") == 0.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/eval/seq_ab/test_baselines.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write minimal implementation**

```python
# eval/seq_ab/baselines.py
"""Trivial 'no-op' floor for seq_ab: the pass-rate of an agent that finishes
immediately with no work. It is the absolute floor every real run must beat.

All current case kinds require the agent to DO something (review/fix/test, or
answer), so a no-op scores 0.0 everywhere. Reported next to every delta so a
'2/3' is visibly above the do-nothing floor. (The other reference — the cheap
always-skill_first arm — is the baseline arm carried by compare.compare_runs.)
"""

NOOP_PASS_BY_KIND: dict[str, float] = {
    "compound": 0.0,
    "control_single": 0.0,
    "control_trivial": 0.0,
}


def noop_floor(kind: str) -> float:
    return NOOP_PASS_BY_KIND.get(kind, 0.0)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/eval/seq_ab/test_baselines.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add eval/seq_ab/baselines.py tests/eval/seq_ab/test_baselines.py
git commit -m "feat(eval): no-op trivial floor for seq_ab cases (WS5)"
```

---

## Task 6: Paired run comparison + win verdict (WS2)

**Files:**
- Create: `eval/seq_ab/compare.py`
- Test: `tests/eval/seq_ab/test_compare.py`

**Interfaces:**
- Consumes: `wilson_interval`, `intervals_disjoint` (Task 3); `noop_floor` (Task 5).
- Produces: `compare_case(rec_a, rec_b, min_trials=3) -> dict`, `compare_runs(baseline, variant, min_trials=3) -> dict`. `rec_*` are per-case records from a results JSON (`id`, `kind`, `pass_count`, `trials_run`, `pass_rate`).

- [ ] **Step 1: Write the failing test**

```python
# tests/eval/seq_ab/test_compare.py
from eval.seq_ab.compare import compare_case, compare_runs


def _rec(cid, kind, passes, n):
    return {"id": cid, "kind": kind, "pass_count": passes, "trials_run": n,
            "pass_rate": round(passes / n, 2) if n else 0.0}


def test_clear_win_has_disjoint_cis():
    base = _rec("c2", "compound", 0, 5)
    var = _rec("c2", "compound", 5, 5)
    out = compare_case(base, var)
    assert out["delta"] > 0
    assert out["cis_disjoint"] is True
    assert out["win"] is True
    assert out["noop_floor"] == 0.0
    assert out["above_noop"] is True


def test_overlapping_cis_is_not_a_win():
    base = _rec("c1", "compound", 2, 3)
    var = _rec("c1", "compound", 3, 3)
    out = compare_case(base, var)
    assert out["win"] is False  # +1/3 with n=3 — CIs overlap


def test_too_few_trials_never_a_win():
    base = _rec("c2", "compound", 0, 2)
    var = _rec("c2", "compound", 2, 2)
    out = compare_case(base, var, min_trials=3)
    assert out["win"] is False


def test_compare_runs_matches_by_id_and_rolls_up():
    baseline = {"cases": [_rec("c2", "compound", 0, 5), _rec("c5", "control_trivial", 5, 5)]}
    variant = {"cases": [_rec("c2", "compound", 5, 5), _rec("c5", "control_trivial", 5, 5)]}
    out = compare_runs(baseline, variant)
    assert out["any_win"] is True
    ids = {c["id"] for c in out["cases"]}
    assert ids == {"c2", "c5"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/eval/seq_ab/test_compare.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write minimal implementation**

```python
# eval/seq_ab/compare.py
"""Compare two seq_ab result runs that differ on exactly ONE axis (e.g. a flag
value). Emits a per-case delta with Wilson CIs, whether the CIs are disjoint, the
no-op floor, and a strict `win` verdict.

A case is a WIN only when: variant rate > baseline rate, BOTH arms ran >= min_trials,
AND their CIs are disjoint. This is the machine check behind the variance policy —
a single green run or an overlapping-CI bump is NOT a win.
"""
from eval.seq_ab.baselines import noop_floor
from eval.seq_ab.variance import intervals_disjoint, wilson_interval


def compare_case(rec_a: dict, rec_b: dict, min_trials: int = 3) -> dict:
    """rec_a = baseline, rec_b = variant. Both are per-case result records."""
    na, nb = rec_a.get("trials_run", 0), rec_b.get("trials_run", 0)
    pa, pb = rec_a.get("pass_count", 0), rec_b.get("pass_count", 0)
    rate_a = rec_a.get("pass_rate", (pa / na) if na else 0.0)
    rate_b = rec_b.get("pass_rate", (pb / nb) if nb else 0.0)
    ci_a = wilson_interval(pa, na)
    ci_b = wilson_interval(pb, nb)
    disjoint = intervals_disjoint(ci_a, ci_b)
    kind = rec_b.get("kind") or rec_a.get("kind") or "unknown"
    floor = noop_floor(kind)
    enough = na >= min_trials and nb >= min_trials
    win = bool(rate_b > rate_a and disjoint and enough)
    return {
        "id": rec_b.get("id") or rec_a.get("id"),
        "kind": kind,
        "baseline_rate": rate_a,
        "variant_rate": rate_b,
        "delta": round(rate_b - rate_a, 3),
        "baseline_ci": [round(x, 3) for x in ci_a],
        "variant_ci": [round(x, 3) for x in ci_b],
        "cis_disjoint": disjoint,
        "enough_trials": enough,
        "noop_floor": floor,
        "above_noop": rate_b > floor,
        "win": win,
    }


def compare_runs(baseline: dict, variant: dict, min_trials: int = 3) -> dict:
    """Match cases by id across two run dicts ({'cases': [...]}) and roll up."""
    by_id_b = {c["id"]: c for c in variant.get("cases", [])}
    cases = []
    for rec_a in baseline.get("cases", []):
        rec_b = by_id_b.get(rec_a["id"])
        if rec_b is None:
            continue
        cases.append(compare_case(rec_a, rec_b, min_trials=min_trials))
    any_win = any(c["win"] for c in cases)
    verdict = "WIN" if any_win else "no measured win"
    return {"cases": cases, "any_win": any_win, "verdict_line": verdict, "min_trials": min_trials}
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/eval/seq_ab/test_compare.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add eval/seq_ab/compare.py tests/eval/seq_ab/test_compare.py
git commit -m "feat(eval): paired-run comparison with strict CI win verdict (WS2)"
```

---

## Task 7: One-flag A/B live wrapper + variance policy doc (WS2)

**Files:**
- Create: `eval/seq_ab/run_flag_ab.sh`
- Create: `docs/superpowers/eval-variance-policy.md`
- Test: `tests/eval/seq_ab/test_compare.py` (add a CLI-entry smoke via `compare_runs` on fixture JSON — no live model)

**Interfaces:**
- Consumes: `compare_runs` (Task 6); `run_seq_ab.py` (writes `results-<mode>.json`).

- [ ] **Step 1: Add a fixture-driven smoke test (no live model)**

```python
# append to tests/eval/seq_ab/test_compare.py
import json
from eval.seq_ab.compare import compare_runs


def test_compare_runs_from_json_files(tmp_path):
    base = {"cases": [{"id": "c2", "kind": "compound", "pass_count": 0, "trials_run": 4, "pass_rate": 0.0}]}
    var = {"cases": [{"id": "c2", "kind": "compound", "pass_count": 4, "trials_run": 4, "pass_rate": 1.0}]}
    (tmp_path / "b.json").write_text(json.dumps(base))
    (tmp_path / "v.json").write_text(json.dumps(var))
    loaded_b = json.loads((tmp_path / "b.json").read_text())
    loaded_v = json.loads((tmp_path / "v.json").read_text())
    out = compare_runs(loaded_b, loaded_v)
    assert out["any_win"] is True
```

- [ ] **Step 2: Run to verify it passes** (compare_runs already exists)

Run: `python -m pytest tests/eval/seq_ab/test_compare.py::test_compare_runs_from_json_files -v`
Expected: PASS

- [ ] **Step 3: Write the live wrapper (shell) + a tiny compare CLI**

Add a `__main__` compare entrypoint to `eval/seq_ab/compare.py` (append at end of file):

```python
if __name__ == "__main__":
    import json as _json
    import sys as _sys

    baseline = _json.loads(open(_sys.argv[1]).read())
    variant = _json.loads(open(_sys.argv[2]).read())
    report = compare_runs(baseline, variant)
    print(f"verdict: {report['verdict_line']}  (min_trials={report['min_trials']})")
    for c in report["cases"]:
        mark = "WIN" if c["win"] else ("~" if c["delta"] else "=")
        print(
            f"  [{mark}] {c['id']}: {c['baseline_rate']} {c['baseline_ci']} -> "
            f"{c['variant_rate']} {c['variant_ci']}  d={c['delta']} "
            f"disjoint={c['cis_disjoint']} floor={c['noop_floor']}"
        )
```

Create `eval/seq_ab/run_flag_ab.sh` (mirrors `run_mode.sh`; RunPod-only paths):

```bash
#!/usr/bin/env bash
# One-flag A/B: run the fixed seq_ab case set twice — FLAG=default vs FLAG=value —
# freezing model+code+fixtures, then print the CI-gated win verdict.
#
# Usage: FLAG=ROUTE_EDIT_TO_REACT DEFAULT=1 VALUE=0 TRIALS=3 bash eval/seq_ab/run_flag_ab.sh
# RunPod-only: hardcodes /workspace/Labmate like run_mode.sh. Adjust paths elsewhere.
set -euo pipefail

: "${FLAG:?set FLAG=NAME}"; : "${DEFAULT:?set DEFAULT=value}"; : "${VALUE:?set VALUE=value}"
TRIALS="${TRIALS:-3}"; MODE="${SEQUENCING_MODE:-skill_first}"
REPO=/workspace/Labmate; cd "$REPO"

run_arm () {  # $1 = flag value, $2 = out-tag
  echo "== arm $FLAG=$1 =="
  bash infrastructure/local/stop.sh >/dev/null 2>&1 || true
  env "$FLAG=$1" SEQUENCING_MODE="$MODE" bash infrastructure/local/start.sh
  sleep 5
  env TRIALS="$TRIALS" python -m eval.seq_ab.run_seq_ab "$MODE"
  mv "eval/seq_ab/results-$MODE.json" "eval/seq_ab/results-flagab-$2.json"
}

run_arm "$DEFAULT" "default"
run_arm "$VALUE" "variant"
python -m eval.seq_ab.compare \
  eval/seq_ab/results-flagab-default.json \
  eval/seq_ab/results-flagab-variant.json
```

Make it executable:

```bash
chmod +x eval/seq_ab/run_flag_ab.sh
```

- [ ] **Step 4: Write the variance policy doc**

```markdown
<!-- docs/superpowers/eval-variance-policy.md -->
# Eval Variance Policy — when a flag default may be called a "win"

A flag default (or a mode, or a numeric constant) may be claimed a **win** ONLY when
all of the following hold, evidenced by a committed results file:

1. **TRIALS >= 3.** Single-shot runs are never a win (c1/c3 flake on the Q4 model —
   "same code, different dice").
2. **Disjoint Wilson 95% CIs.** The variant arm's `pass_rate_ci` must not overlap the
   baseline arm's. An overlapping-CI bump (e.g. +1/3 at n=3) is NOT a win.
3. **Above the trivial baseline.** The variant must beat the no-op floor
   (`eval/seq_ab/baselines.py`) and be reported next to it.
4. **Single-axis capture.** Baseline and variant differ on exactly ONE axis, under the
   same model + git sha + fixtures (see the controlled-comparison protocol).

The machine check is `eval/seq_ab/compare.py::compare_runs` (`win=True` requires 1, 2,
and rate>baseline). Produce the two arms with `eval/seq_ab/run_flag_ab.sh`.

**Flags whose defaults do NOT yet meet this bar** (see the audit spec, Part A2) are
intuition/anecdotal and must not be described as measured wins until an A/B is run.
Retiring vs measuring them is the backlog in the spec, Part E.
```

- [ ] **Step 5: Commit**

```bash
git add eval/seq_ab/run_flag_ab.sh eval/seq_ab/compare.py docs/superpowers/eval-variance-policy.md tests/eval/seq_ab/test_compare.py
git commit -m "feat(eval): one-flag A/B wrapper + variance-win policy (WS2)"
```

---

## Task 8: Provenance header — build/capture/compare (WS3)

**Files:**
- Create: `eval/provenance.py`
- Test: `tests/eval/test_provenance.py`

**Interfaces:**
- Produces: `TRACKED_FLAGS: list[str]`, `capture_env_flags(environ, names=TRACKED_FLAGS) -> dict`, `build_provenance(model, git_sha, captured_at, env) -> dict`, `compare_provenance(a, b) -> list[str]`, `current_git_sha() -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/eval/test_provenance.py
from eval.provenance import (
    build_provenance, capture_env_flags, compare_provenance, TRACKED_FLAGS,
)


def test_capture_env_flags_picks_only_tracked():
    env = {"SEQUENCING_MODE": "react", "ROUTE_EDIT_TO_REACT": "1", "HOME": "/x"}
    snap = capture_env_flags(env)
    assert snap["SEQUENCING_MODE"] == "react"
    assert snap["ROUTE_EDIT_TO_REACT"] == "1"
    assert "HOME" not in snap


def test_build_provenance_shape():
    p = build_provenance("gemma-4-12b-it-UD-Q4_K_XL.gguf", "abc1234", "2026-07-02T10:00:00", {"X": "1"})
    assert p["model"].endswith(".gguf")
    assert p["git_sha"] == "abc1234"
    assert p["captured_at"] == "2026-07-02T10:00:00"
    assert p["env"] == {"X": "1"}


def test_compare_provenance_flags_axis_drift():
    a = build_provenance("31b.gguf", "aaa", "t1", {})
    b = build_provenance("12b.gguf", "bbb", "t2", {})
    warnings = compare_provenance(a, b)
    assert any("model" in w for w in warnings)
    assert any("git_sha" in w for w in warnings)


def test_compare_provenance_clean_when_same():
    a = build_provenance("12b.gguf", "aaa", "t1", {})
    b = build_provenance("12b.gguf", "aaa", "t2", {})
    assert compare_provenance(a, b) == []


def test_tracked_flags_covers_the_load_bearing_ones():
    for f in ("SEQUENCING_MODE", "ROUTE_EDIT_TO_REACT", "MAX_GOAL_ATTEMPTS"):
        assert f in TRACKED_FLAGS
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/eval/test_provenance.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write minimal implementation**

```python
# eval/provenance.py
"""Provenance header for eval result files: model + git sha + capture time + the
tracked env-flag snapshot. Lets a reader (and compare_provenance) tell whether two
result files were captured under the SAME conditions or differ on >1 axis.

Pure builders take all inputs as arguments (no wall-clock, no subprocess) so they are
deterministic and testable. current_git_sha() is the only impure helper.
"""
from __future__ import annotations

import subprocess

# The behavioral flags worth snapshotting (audit spec, Part A2). Extend as flags land.
TRACKED_FLAGS: list[str] = [
    "SEQUENCING_MODE",
    "ROUTE_EDIT_TO_REACT",
    "LABMATE_REFUND_REPEAT_LOAD_SKILL",
    "LOOP_REPEAT_LIMIT_MUTATING",
    "LABMATE_MAX_ITERATIONS_EDIT",
    "LABMATE_TOOL_RESULT_BUDGET",
    "LABMATE_GOAL_DEADLINE_S",
    "LABMATE_NOPROGRESS_LIMIT",
    "MAX_VERIFY_NUDGES",
    "MAX_GOAL_ATTEMPTS",
    "ENABLE_CONDITIONAL_GATES",
    "ENABLE_MESSAGE_REPAIR",
    "ENABLE_FINALIZE_REVISION",
    "ENABLE_LOOP_CHECKPOINT",
    "ENABLE_ROUTING_PREGATE",
]


def capture_env_flags(environ, names: list[str] = TRACKED_FLAGS) -> dict:
    """Snapshot only the tracked flags that are actually set in `environ`."""
    return {k: environ[k] for k in names if k in environ}


def build_provenance(model: str, git_sha: str, captured_at: str, env: dict) -> dict:
    return {"model": model, "git_sha": git_sha, "captured_at": captured_at, "env": env}


def compare_provenance(a: dict, b: dict) -> list[str]:
    """Warnings when two provenance headers differ on an axis that should be frozen."""
    warnings = []
    for axis in ("model", "git_sha"):
        if a.get(axis) != b.get(axis):
            warnings.append(f"{axis} differs: {a.get(axis)!r} != {b.get(axis)!r}")
    return warnings


def current_git_sha() -> str:
    """Short HEAD sha, or 'unknown' if git is unavailable."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True
        ).strip()
    except Exception:  # noqa: BLE001
        return "unknown"
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/eval/test_provenance.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add eval/provenance.py tests/eval/test_provenance.py
git commit -m "feat(eval): result-file provenance header + drift compare (WS3)"
```

---

## Task 9: Stamp provenance into seq_ab + protocol doc + retire stale baselines (WS3)

**Files:**
- Modify: `eval/seq_ab/run_seq_ab.py` (`main`)
- Create: `docs/superpowers/eval-controlled-comparison-protocol.md`
- Rename: `eval/seq_ab/results-skill_first.json` → `results-skill_first.31b.ref.json`; `results-react.json` → `results-react.31b.ref.json`
- Test: `tests/eval/test_provenance.py` (add a stamp-shape assertion importing the helper the runner uses)

**Interfaces:**
- Consumes: `build_provenance`, `capture_env_flags`, `current_git_sha` (Task 8).

- [ ] **Step 1: Add the failing test (runner uses the tracked snapshot)**

```python
# append to tests/eval/test_provenance.py
import os
from eval.provenance import capture_env_flags


def test_runner_snapshot_reflects_process_env(monkeypatch):
    monkeypatch.setenv("SEQUENCING_MODE", "skill_first")
    monkeypatch.setenv("ROUTE_EDIT_TO_REACT", "1")
    snap = capture_env_flags(os.environ)
    assert snap.get("SEQUENCING_MODE") == "skill_first"
    assert snap.get("ROUTE_EDIT_TO_REACT") == "1"
```

- [ ] **Step 2: Run to verify it passes** (function exists from Task 8)

Run: `python -m pytest tests/eval/test_provenance.py::test_runner_snapshot_reflects_process_env -v`
Expected: PASS

- [ ] **Step 3: Wire the stamp into `run_seq_ab.py` `main`**

Add imports at the top of `eval/seq_ab/run_seq_ab.py`:

```python
from eval.provenance import build_provenance, capture_env_flags, current_git_sha
```

In `main`, replace the output-dict line with a version that stamps provenance:

```python
    provenance = build_provenance(
        model=os.path.basename(os.getenv("MODEL", "")) or "unknown",
        git_sha=current_git_sha(),
        captured_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        env=capture_env_flags(os.environ),
    )
    out = {
        "mode": MODE,
        "trials": TRIALS,
        "provenance": provenance,
        "metric_meaning": seq_ab_meaning_block(),
        "cases": [],
    }
```

- [ ] **Step 4: Retire the stale 31B baselines + write the protocol doc**

```bash
git mv eval/seq_ab/results-skill_first.json eval/seq_ab/results-skill_first.31b.ref.json
git mv eval/seq_ab/results-react.json eval/seq_ab/results-react.31b.ref.json
```

```markdown
<!-- docs/superpowers/eval-controlled-comparison-protocol.md -->
# Controlled-Comparison Protocol (freeze model+code, vary one axis)

Every A/B comparison must **freeze model + git sha + fixtures** and **vary exactly one
axis**. Both result files carry a provenance header (`eval/provenance.py`); compare
them with `compare_provenance` before trusting a delta.

## Rule
1. Capture baseline and variant back-to-back on the SAME pod, SAME `MODEL` gguf, SAME
   `git rev-parse HEAD`, SAME fixtures (reset per trial by `run_seq_ab.py`).
2. The ONLY difference between the two runs is the one axis under test
   (`SEQUENCING_MODE`, one flag value, etc.).
3. Stamp `provenance` into both files (automatic in `run_seq_ab.py`).
4. `compare_provenance(a, b)` MUST return `[]` (no model/sha drift) before the delta
   counts. A non-empty result means the comparison mixes >1 axis — discard it.

## Known-bad comparisons this retires
- **current 12B code vs committed 31B `results-*.json`** — moved model + code at once.
  The stale files are now `results-*.31b.ref.json` (history only; never compared to a
  12B run without a `compare_provenance` warning).
- **`results-*.ref.json` (pre-fix) vs post-fix** — moved a bundle of code changes.
- **routing cases generated on 31B, evaluated on 12B** — generation/eval model drift.

## Known-good template (copy this)
- `skill_first` vs `react`: paired, same commit + model + fixtures, only
  `SEQUENCING_MODE` varied.

## Re-baseline task (run on the GPU host)
Recapture the 12B baseline under current HEAD:
```bash
TRIALS=3 bash eval/seq_ab/run_mode.sh skill_first
TRIALS=3 bash eval/seq_ab/run_mode.sh react
git add eval/seq_ab/results-skill_first.json eval/seq_ab/results-react.json
git commit -m "chore(eval): re-baseline seq_ab on 12B under HEAD"
```
The new files carry a 12B provenance header; the `.31b.ref.json` files stay for history.
```

- [ ] **Step 5: Commit**

```bash
git add eval/seq_ab/run_seq_ab.py docs/superpowers/eval-controlled-comparison-protocol.md tests/eval/test_provenance.py
git add eval/seq_ab/results-skill_first.31b.ref.json eval/seq_ab/results-react.31b.ref.json
git commit -m "feat(eval): stamp provenance + retire stale 31B baselines + protocol (WS3)"
```

> **NOTE (needs GPU host):** the actual 12B re-capture (the commands in the protocol
> doc) runs on the pod and is NOT part of this commit — it produces the new
> `results-*.json`. Do it when the host is available; it is the one step this plan
> cannot do offline.

---

## Task 10: Routing trivial baselines (majority + random) (WS5)

**Files:**
- Create: `eval/routing_metrics.py`
- Test: `tests/eval/test_routing_metrics.py`

**Interfaces:**
- Produces: `majority_class_accuracy(cases: list[dict]) -> float`, `random_baseline_accuracy(cases: list[dict], n_skills: int) -> float`

- [ ] **Step 1: Write the failing test**

```python
# tests/eval/test_routing_metrics.py
import pytest
from eval.routing_metrics import majority_class_accuracy, random_baseline_accuracy


def test_majority_class_is_modal_share():
    cases = [
        {"expected": "code-review"}, {"expected": "code-review"},
        {"expected": "code-review"}, {"expected": "test-gen"},
    ]
    # modal label 'code-review' covers 3/4
    assert majority_class_accuracy(cases) == pytest.approx(0.75)


def test_majority_class_empty_is_zero():
    assert majority_class_accuracy([]) == 0.0


def test_random_baseline_is_one_over_actions():
    # 10 skills + 1 decline option -> 1/11 per case
    cases = [{"expected": "a"}, {"expected": "none"}]
    assert random_baseline_accuracy(cases, n_skills=10) == pytest.approx(1 / 11)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/eval/test_routing_metrics.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write minimal implementation**

```python
# eval/routing_metrics.py
"""Trivial baselines + leakage-inflation for the routing eval.

These give a reader a reference point next to the headline accuracy: is 0.80 good, or
does always-guess-the-commonest-skill already get 0.6? And is the number inflated by
evaluating on model-generated (leaked) cases?
"""
from collections import Counter


def majority_class_accuracy(cases: list[dict]) -> float:
    """Accuracy of always predicting the single most common `expected` label."""
    if not cases:
        return 0.0
    labels = [c["expected"] for c in cases]
    modal, _ = Counter(labels).most_common(1)[0]
    return sum(1 for x in labels if x == modal) / len(labels)


def random_baseline_accuracy(cases: list[dict], n_skills: int) -> float:
    """Expected accuracy of picking uniformly among the n_skills skills + 1 decline
    action. Each case has exactly one correct action, so P(correct) = 1/(n_skills+1)."""
    if not cases or n_skills < 0:
        return 0.0
    return 1.0 / (n_skills + 1)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/eval/test_routing_metrics.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add eval/routing_metrics.py tests/eval/test_routing_metrics.py
git commit -m "feat(eval): majority + random trivial baselines for routing (WS5)"
```

---

## Task 11: Leakage-inflation estimate (WS4)

**Files:**
- Modify: `eval/routing_metrics.py`
- Test: `tests/eval/test_routing_metrics.py` (extend)

**Interfaces:**
- Produces: `inflation_estimate(generated_overall: float, heldout_overall: float) -> float`

- [ ] **Step 1: Add the failing test**

```python
# append to tests/eval/test_routing_metrics.py
from eval.routing_metrics import inflation_estimate


def test_inflation_is_generated_minus_heldout():
    assert inflation_estimate(0.90, 0.72) == pytest.approx(0.18)


def test_inflation_can_be_negative():
    assert inflation_estimate(0.70, 0.75) == pytest.approx(-0.05)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/eval/test_routing_metrics.py -k inflation -v`
Expected: FAIL (`ImportError: cannot import name 'inflation_estimate'`)

- [ ] **Step 3: Implement — append to `eval/routing_metrics.py`**

```python
def inflation_estimate(generated_overall: float, heldout_overall: float) -> float:
    """Leakage inflation = accuracy on model-generated cases minus accuracy on the
    held-out (non-model-generated) slice. Positive => the headline is inflated by cases
    that echo SKILL.md wording the router also sees."""
    return round(generated_overall - heldout_overall, 4)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/eval/test_routing_metrics.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add eval/routing_metrics.py tests/eval/test_routing_metrics.py
git commit -m "feat(eval): leakage-inflation estimate (generated - heldout) (WS4)"
```

---

## Task 12: Held-out anti-leakage routing slice (WS4)

**Files:**
- Create: `eval/routing_eval.heldout.jsonl`
- Test: `tests/eval/test_heldout_slice.py`

**Interfaces:**
- Produces: a JSONL file, one case per line, schema `{"id","task","expected","cluster","source":"heldout","kind":"skill"}`; `expected` is a skill name in the catalog or `"none"`.

**Authoring rubric (this is the Claude-drafts / user-curates step):** write **30–40** cases
in **real user voice** (imperative, varied length, NO echo of SKILL.md description wording —
that is the whole point of a leakage-free slice). Cover: (a) ≥1 positive per major skill
cluster, weighted toward **rare/long-tail skills** that get few generated cases; (b) **6–8
negatives** (`expected:"none"` — arithmetic, rephrase, general-knowledge); (c) **5–6 hard
boundary** cases near two skills. `id` prefix `ho_`. **Never** run this file through
`extend_eval.py`. After drafting, the user prunes/edits before it is locked.

- [ ] **Step 1: Write the failing schema/separateness test**

```python
# tests/eval/test_heldout_slice.py
import json
from pathlib import Path

HELDOUT = Path("eval/routing_eval.heldout.jsonl")
SEED = Path("eval/routing_eval.seed.jsonl")
WORKING = Path("eval/routing_eval.jsonl")


def _load(p):
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def test_heldout_exists_and_is_sized():
    cases = _load(HELDOUT)
    assert 30 <= len(cases) <= 40


def test_heldout_schema_and_source_tag():
    for c in _load(HELDOUT):
        assert c["source"] == "heldout"
        assert c["id"].startswith("ho_")
        assert c["expected"]
        assert c["task"].strip()


def test_heldout_has_negatives_and_is_disjoint_from_seed_and_working():
    cases = _load(HELDOUT)
    assert sum(1 for c in cases if c["expected"] == "none") >= 6
    tasks = {c["task"].strip().lower() for c in cases}
    for other in (SEED, WORKING):
        if other.exists():
            overlap = tasks & {c["task"].strip().lower() for c in _load(other)}
            assert not overlap, f"held-out overlaps {other}: {overlap}"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/eval/test_heldout_slice.py -v`
Expected: FAIL (file missing)

- [ ] **Step 3: Author the slice** (starter records shown; expand to 30–40 per the rubric, then user-curate)

```json
{"id": "ho_01", "task": "The retry helper in our client keeps double-counting attempts — can you pin down which function is wrong?", "expected": "repo-fault-localize", "cluster": "debugging", "source": "heldout", "kind": "skill"}
{"id": "ho_02", "task": "I need a second pair of eyes on this pull request before I merge it.", "expected": "code-review", "cluster": "review", "source": "heldout", "kind": "skill"}
{"id": "ho_03", "task": "Turn this quarter's benchmark writeup into a deck for Thursday's meeting.", "expected": "paper-to-slides", "cluster": "docs", "source": "heldout", "kind": "skill"}
{"id": "ho_04", "task": "What's 384 divided by 12?", "expected": "none", "cluster": "negative", "source": "heldout", "kind": "skill"}
{"id": "ho_05", "task": "Make this paragraph sound a little less stiff, same meaning.", "expected": "none", "cluster": "negative", "source": "heldout", "kind": "skill"}
{"id": "ho_06", "task": "Pull the current changelog for the library off their site.", "expected": "web-search", "cluster": "external", "source": "heldout", "kind": "skill"}
```

> Expand to the full 30–40 following the rubric above. Verify each `expected` is a real
> skill under `services/skills/*/SKILL.md` (or `"none"`). Keep phrasings in user voice,
> not description echoes.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/eval/test_heldout_slice.py -v`
Expected: PASS (3 passed) — once the file has 30–40 valid, disjoint cases.

- [ ] **Step 5: Commit**

```bash
git add eval/routing_eval.heldout.jsonl tests/eval/test_heldout_slice.py
git commit -m "feat(eval): held-out non-model-generated routing slice (WS4)"
```

---

## Task 13: Wire baselines + held-out + inflation into the routing report (WS4+WS5)

**Files:**
- Modify: `eval/run_routing_eval.py` (`write_reports` signature + `main` for `--heldout`)
- Test: `tests/eval/test_routing_metrics.py` (add a report-assembly test)

**Interfaces:**
- Consumes: `majority_class_accuracy`, `random_baseline_accuracy`, `inflation_estimate` (Tasks 10–11); `evaluate`/`summarize` (existing).
- Produces: `write_reports(results, summary, report_dir, repeats, baselines=None, leakage=None) -> Path`

- [ ] **Step 1: Add the failing report-assembly test**

```python
# append to tests/eval/test_routing_metrics.py
from pathlib import Path
from eval.run_routing_eval import write_reports


def test_report_includes_baselines_and_leakage(tmp_path):
    summary = {
        "overall": 0.90, "n": 20, "mean_stability": 1.0, "by_cluster": {},
        "by_skill": {}, "by_kind": {}, "false_positive_rate": None, "confusion": [],
    }
    baselines = {"majority_class": 0.30, "random": 0.09}
    leakage = {"generated_overall": 0.90, "heldout_overall": 0.72, "inflation": 0.18}
    md = write_reports([], summary, str(tmp_path), 3, baselines=baselines, leakage=leakage)
    body = Path(md).read_text()
    assert "majority" in body.lower() and "0.30" in body
    assert "inflation" in body.lower() and "0.18" in body
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/eval/test_routing_metrics.py::test_report_includes_baselines_and_leakage -v`
Expected: FAIL (`write_reports() got an unexpected keyword argument 'baselines'`)

- [ ] **Step 3: Implement**

In `eval/run_routing_eval.py`, add the import at the top:

```python
from eval.routing_metrics import (
    inflation_estimate, majority_class_accuracy, random_baseline_accuracy,
)
```

Change the `write_reports` signature and append the new sections before `md_path = ...`:

```python
def write_reports(results, summary, report_dir, repeats, baselines=None, leakage=None):
```

Insert, right after the `false-positive rate` block in the `lines` list (i.e. after the
opening summary bullets, before `## Per-kind`):

```python
    if baselines:
        lines += [
            "## Trivial baselines (interpret the accuracy against these)",
            "",
            f"- majority-class: {baselines['majority_class']:.3f}",
            f"- random (1/(skills+1)): {baselines['random']:.3f}",
            "",
        ]
    if leakage:
        lines += [
            "## Leakage check (generated vs held-out)",
            "",
            f"- generated-set overall: {leakage['generated_overall']:.3f}",
            f"- held-out overall: {leakage['heldout_overall']:.3f}",
            f"- inflation (generated - heldout): {leakage['inflation']:.3f}",
            "",
        ]
```

In `main`, after `summary = summarize(results)` and before `md = write_reports(...)`,
compute the baselines and (optionally) the held-out slice:

```python
    baselines = {
        "majority_class": majority_class_accuracy(cases),
        "random": random_baseline_accuracy(cases, n_skills=len(catalog)),
    }

    leakage = None
    if args.heldout:
        heldout_cases = [
            json.loads(line)
            for line in Path(args.heldout).read_text().splitlines()
            if line.strip()
        ]
        heldout_results = asyncio.run(
            evaluate(
                heldout_cases, client, args.model, catalog, args.repeats,
                args.select_attempts, args.temperature, args.concurrency,
                hosted_tools=hosted_tools, catalog_mode=args.catalog_mode,
            )
        )
        heldout_summary = summarize(heldout_results)
        leakage = {
            "generated_overall": summary["overall"],
            "heldout_overall": heldout_summary["overall"],
            "inflation": inflation_estimate(summary["overall"], heldout_summary["overall"]),
        }
```

Add the `--heldout` arg next to the other `ap.add_argument` calls:

```python
    ap.add_argument("--heldout", help="held-out, non-model-generated slice (leakage check)")
```

And update the `write_reports` call in `main`:

```python
    md = write_reports(results, summary, args.report, args.repeats, baselines=baselines, leakage=leakage)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/eval/test_routing_metrics.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add eval/run_routing_eval.py tests/eval/test_routing_metrics.py
git commit -m "feat(eval): routing report shows baselines + held-out leakage (WS4/WS5)"
```

---

## Task 14: Long-tail seq_ab cases + routing long-tail + CLAUDE.md pointers (WS5)

**Files:**
- Modify: `eval/seq_ab/run_seq_ab.py` (`FIXTURES`, `CASES`)
- Modify: `eval/routing_eval.jsonl` (append long-tail working-set cases — NOT the seed)
- Modify: `CLAUDE.md` (eval-section pointers)
- Test: `tests/eval/seq_ab/test_longtail_cases.py`, `tests/eval/test_heldout_slice.py` (reuse working-set loader)

**Interfaces:**
- Consumes: nothing new; extends existing case lists.

- [ ] **Step 1: Write the failing consistency test**

```python
# tests/eval/seq_ab/test_longtail_cases.py
from eval.seq_ab.run_seq_ab import CASES, FIXTURES


def test_has_added_harder_compound_cases():
    ids = {c["id"] for c in CASES}
    assert "c6_multiedit_fix" in ids
    assert sum(1 for c in CASES if c["kind"] == "compound") >= 4


def test_every_case_fixture_path_exists_in_fixtures():
    # Any /workspace/ab_*.py a case names must be defined in FIXTURES.
    import re
    for c in CASES:
        for path in re.findall(r"/workspace/ab_\w+\.py", c["task"]):
            assert path in FIXTURES, f"{c['id']} references undefined fixture {path}"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/eval/seq_ab/test_longtail_cases.py -v`
Expected: FAIL (`c6_multiedit_fix` not present)

- [ ] **Step 3: Implement — add one harder compound case + its fixture**

In `eval/seq_ab/run_seq_ab.py`, add a fixture to `FIXTURES` (a bug needing TWO edits — a
harder, longer-tail multi-edit fix):

```python
    "/workspace/ab_multi.py": (
        'def normalize(xs):\n'
        '    """Scale a list of numbers to sum to 1.0. Empty list -> []."""\n'
        '    total = 0\n'
        '    for x in xs:\n'
        '        total = x            # bug 1: assignment, not accumulation\n'
        '    return [x / total for x in xs]   # bug 2: no empty-list guard (ZeroDivisionError)\n'
    ),
```

Add the case to `CASES` (after `c3`):

```python
    {"id": "c6_multiedit_fix", "kind": "compound",
     "task": "Fix the two bugs in /workspace/ab_multi.py (the running total and the empty-list crash), then write and run a test that proves normalize works and handles []."},
```

- [ ] **Step 4: Append routing long-tail cases to the working set** (NOT the seed)

Append 6–8 rare-skill / hard-boundary cases to `eval/routing_eval.jsonl`, each tagged
`"source": "longtail"`, e.g.:

```json
{"id": "lt_01", "task": "Trace every function that ends up calling the websocket auth handshake and show me the chain.", "expected": "repo-graph", "cluster": "codenav", "source": "longtail", "kind": "skill"}
{"id": "lt_02", "task": "Check that every citation key in this draft actually resolves to a bib entry.", "expected": "citation-check", "cluster": "academic", "source": "longtail", "kind": "skill"}
```

> Add 6–8 total, weighted to skills with the fewest generated cases (audit A5). Verify
> each `expected` is in the live catalog. Do not touch `routing_eval.seed.jsonl`.

- [ ] **Step 5: Update CLAUDE.md pointers + run the full eval suite + commit**

Add a short note under the eval section of `CLAUDE.md` (near §5/§9) pointing to the new
tooling — one line each:

```markdown
- **Eval methodology (2026-07-02):** metric captions (`eval/metric_meaning.py`), Wilson-CI
  win check (`eval/seq_ab/{variance,compare}.py` + `run_flag_ab.sh`, policy in
  `docs/superpowers/eval-variance-policy.md`), provenance headers +
  controlled-comparison protocol (`eval/provenance.py` +
  `docs/superpowers/eval-controlled-comparison-protocol.md`), held-out leakage slice
  (`eval/routing_eval.heldout.jsonl` + `--heldout`), trivial baselines
  (`eval/routing_metrics.py`, `eval/seq_ab/baselines.py`). Stale 31B baselines archived
  as `results-*.31b.ref.json`; re-capture on 12B per the protocol doc.
```

Run the full eval test suite:

Run: `python -m pytest tests/eval/ -v`
Expected: PASS (all eval tests green)

```bash
git add eval/seq_ab/run_seq_ab.py eval/routing_eval.jsonl CLAUDE.md tests/eval/seq_ab/test_longtail_cases.py
git commit -m "feat(eval): long-tail cases + CLAUDE.md eval-methodology pointers (WS5)"
```

---

## Post-implementation (run once, after all tasks)

- [ ] Full eval suite green: `python -m pytest tests/eval/ -v`
- [ ] Orchestrator suite unaffected (no harness files touched): `python -m pytest tests/services/orchestrator/ -q` → all green
- [ ] **On the GPU host:** re-capture the 12B baseline (Task 9 note) and run one
  `eval/seq_ab/run_flag_ab.sh` on `ROUTE_EDIT_TO_REACT` to confirm the CI-win check
  reproduces the known win with disjoint intervals.
- [ ] Open a PR from `docs/eval-methodology-audit` (spec + plan + all WS commits); the
  held-out slice and long-tail cases get a final human-curation pass in review.

## Deferred to backlog (spec Part E — do NOT do here)
Per-flag variance sweeps (`MAX_GOAL_ATTEMPTS`, `LABMATE_TOOL_RESULT_BUDGET`, …),
`LABMATE_REFUND_REPEAT_LOAD_SKILL` isolation A/B, measure-then-retire on the six
OFF/opt-in flags, systematizing the honesty judge, enabling `ENABLE_ROUTING_PREGATE`.
These use the WS2 one-flag A/B once it exists; they are not part of this plan.
