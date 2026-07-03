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
