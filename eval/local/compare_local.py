"""A/B compare two eval/local report-<mode>.json files via the existing
Wilson-CI tooling (eval/seq_ab/compare.py + eval/seq_ab/variance.py).

eval/seq_ab/compare.py and eval/seq_ab/variance.py do NOT import redis (only
eval/seq_ab/run_seq_ab.py's live-driver does), so this module imports them
directly rather than re-implementing Wilson-CI math — verified by the same
no-redis grep this harness runs on eval/local/ (see eval/local/README.md).

The seq_ab tooling operates on pre-aggregated per-case records
({"id", "kind", "pass_count", "trials_run", "pass_rate"}); this module's job
is reducing a local report's scored trajectory list (one row per trial per
case) down to that shape, using the tagger's OUTCOME tags as the pass
criterion — a trial "passes" when it is tagged `outcome:completed` (a
non-fabricating success) and, for cases whose trajectory made an edit,
ideally `edited+verified` too. Per the brief we score on the OUTCOME pass-rate
(fraction outcome:completed), which already excludes doom-loop/cancelled/
errored trials via `tag_run`'s mutually-exclusive outcome tag.
"""

from __future__ import annotations

import json
from typing import Any

from eval.seq_ab.compare import compare_runs


def _case_kind(case_id: str, cases_meta: dict[str, str] | None) -> str:
    if cases_meta and case_id in cases_meta:
        return cases_meta[case_id]
    return "unknown"


def report_to_case_records(
    report: dict[str, Any],
    *,
    cases_meta: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Reduce a report-<mode>.json (scored trajectories) into seq_ab's
    {"cases": [{"id","kind","pass_count","trials_run","pass_rate"}]} shape.

    `cases_meta` optionally maps case_id -> kind (e.g. from eval.local.cases.CASES)
    so the noop_floor lookup in compare_case is meaningful; defaults to "unknown"
    (noop_floor("unknown") is still a defined, if uninformative, floor).
    """
    by_case: dict[str, list[dict]] = {}
    for traj in report.get("trajectories", []):
        by_case.setdefault(traj["case_id"], []).append(traj)

    cases = []
    for case_id, trajs in by_case.items():
        tags_per_traj = [t.get("tags") or [] for t in trajs]
        pass_count = sum(1 for tags in tags_per_traj if "outcome:completed" in tags)
        trials_run = len(trajs)
        pass_rate = round(pass_count / trials_run, 2) if trials_run else 0.0
        cases.append(
            {
                "id": case_id,
                "kind": _case_kind(case_id, cases_meta),
                "pass_count": pass_count,
                "trials_run": trials_run,
                "pass_rate": pass_rate,
            }
        )
    return {"cases": cases}


def compare_reports(
    baseline_report: dict[str, Any],
    variant_report: dict[str, Any],
    *,
    cases_meta: dict[str, str] | None = None,
    min_trials: int = 5,
) -> dict[str, Any]:
    """Compare two eval/local report-<mode>.json dicts on outcome pass-rate."""
    baseline = report_to_case_records(baseline_report, cases_meta=cases_meta)
    variant = report_to_case_records(variant_report, cases_meta=cases_meta)
    return compare_runs(baseline, variant, min_trials=min_trials)


def _load_cases_meta() -> dict[str, str] | None:
    try:
        from eval.local.cases import CASES

        return {c["id"]: c["kind"] for c in CASES}
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    import sys

    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) < 2:
        print(
            "usage: python -m eval.local.compare_local <baseline-report.json> <variant-report.json>",
            file=sys.stderr,
        )
        return 2

    baseline = json.loads(open(argv[0]).read())
    variant = json.loads(open(argv[1]).read())
    result = compare_reports(baseline, variant, cases_meta=_load_cases_meta())

    print(f"verdict: {result['verdict_line']}  (min_trials={result['min_trials']})")
    for c in result["cases"]:
        mark = "WIN" if c["win"] else ("~" if c["delta"] else "=")
        print(
            f"  [{mark}] {c['id']}: {c['baseline_rate']} {c['baseline_ci']} -> "
            f"{c['variant_rate']} {c['variant_ci']}  d={c['delta']} "
            f"disjoint={c['cis_disjoint']} floor={c['noop_floor']}"
        )

    # Eyeball KPI deltas the brief calls out explicitly: fabricated_rate must
    # stay 0, doom_loop_rate/tool_success_rate/calls must not regress.
    b_kpis = baseline.get("kpis", {})
    v_kpis = variant.get("kpis", {})
    print("\nKPI deltas (baseline -> variant):")
    for key in (
        "fabricated_rate",
        "doom_loop_rate",
        "tool_success_rate",
        "avg_llm_calls",
        "ok_rate",
    ):
        print(f"  {key}: {b_kpis.get(key)} -> {v_kpis.get(key)}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
