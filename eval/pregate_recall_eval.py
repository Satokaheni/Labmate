"""Pre-gate recall eval — sweep similarity thresholds over routing eval cases.

Measures the SkillPreGate's FALSE-SKIP rate (recall regression risk) and
CORRECT-SKIP rate (latency win) across a range of thresholds so the operator
can pick the highest threshold where per-skill false-skip rate ≤ 0.05.

Usage (on host, after services start):
    python eval/pregate_recall_eval.py \
        --eval eval/routing_eval.jsonl \
        --skills-dir services/skills \
        --thresholds 0.20,0.25,0.30,0.35,0.40

Step 5 gate (see task-0.3-brief.md):
    This output MUST be reviewed before flipping ENABLE_ROUTING_PREGATE=1 and
    setting PREGATE_SIM_THRESHOLD to the recommended threshold.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Pure aggregation core (unit-tested in tests/eval/test_pregate_recall_eval.py)
# ---------------------------------------------------------------------------


def summarize_skips(rows: list[dict], thresholds: list[float]) -> list[dict]:
    """Aggregate false-skip and correct-skip rates per threshold.

    Args:
        rows: list of {"expected": str, "max_sim": float}.
              expected == "none" means no skill should match.
        thresholds: list of similarity thresholds to evaluate.

    Returns:
        list of dicts, one per threshold:
            {
                "threshold": float,
                "false_skip_rate": float,   # skill rows skipped incorrectly
                "correct_skip_rate": float, # none rows skipped correctly
                "per_skill_false_skip": {skill: rate},
                "n_skill": int,
                "n_none": int,
            }
    """
    skill_rows = [r for r in rows if r.get("expected") != "none"]
    none_rows = [r for r in rows if r.get("expected") == "none"]
    n_skill = len(skill_rows)
    n_none = len(none_rows)

    results = []
    for threshold in thresholds:
        false_skips = [r for r in skill_rows if r["max_sim"] < threshold]
        correct_skips = [r for r in none_rows if r["max_sim"] < threshold]

        false_skip_rate = len(false_skips) / n_skill if n_skill else 0.0
        correct_skip_rate = len(correct_skips) / n_none if n_none else 0.0

        # per-skill breakdown: group false-skips by expected skill
        per_skill_counts: dict[str, int] = {}
        per_skill_total: dict[str, int] = {}
        for r in skill_rows:
            skill = r["expected"]
            per_skill_total[skill] = per_skill_total.get(skill, 0) + 1
        for r in false_skips:
            skill = r["expected"]
            per_skill_counts[skill] = per_skill_counts.get(skill, 0) + 1

        per_skill_false_skip: dict[str, float] = {
            skill: per_skill_counts.get(skill, 0) / total
            for skill, total in per_skill_total.items()
        }

        results.append(
            {
                "threshold": threshold,
                "false_skip_rate": false_skip_rate,
                "correct_skip_rate": correct_skip_rate,
                "per_skill_false_skip": per_skill_false_skip,
                "n_skill": n_skill,
                "n_none": n_none,
            }
        )

    return results


# ---------------------------------------------------------------------------
# Live harness (runs on host with real SkillRunner + SkillPreGate)
# ---------------------------------------------------------------------------

# Built-in no-match conversational probes to exercise the correct-skip win
_BUILTIN_NO_MATCH_PROBES = [
    "what is the traveling salesman problem",
    "what is the capital of France",
    "what is 2+2",
]


def _load_cases(eval_path: Path) -> list[dict]:
    """Load JSON-lines routing eval cases."""
    cases = []
    with eval_path.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


def _recommend_threshold(results: list[dict], max_false_skip: float = 0.05) -> float | None:
    """Return the highest threshold where every per-skill false-skip rate ≤ max_false_skip."""
    candidate = None
    for r in results:
        all_ok = all(v <= max_false_skip for v in r["per_skill_false_skip"].values())
        # Also require the aggregate false_skip_rate ≤ max_false_skip
        if all_ok and r["false_skip_rate"] <= max_false_skip:
            if candidate is None or r["threshold"] > candidate:
                candidate = r["threshold"]
    return candidate


def _print_table(results: list[dict]) -> None:
    """Print a readable table of threshold sweep results."""
    col_w = 12
    header = (
        f"{'Threshold':>{col_w}}"
        f"{'FalseSkip%':>{col_w}}"
        f"{'CorrectSkip%':>{col_w}}"
        f"{'n_skill':>{col_w}}"
        f"{'n_none':>{col_w}}"
    )
    print("\n" + "=" * (col_w * 5))
    print("Pre-gate recall eval — threshold sweep")
    print("=" * (col_w * 5))
    print(header)
    print("-" * (col_w * 5))
    for r in results:
        print(
            f"{r['threshold']:>{col_w}.2f}"
            f"{r['false_skip_rate'] * 100:>{col_w}.1f}"
            f"{r['correct_skip_rate'] * 100:>{col_w}.1f}"
            f"{r['n_skill']:>{col_w}}"
            f"{r['n_none']:>{col_w}}"
        )
    print("=" * (col_w * 5))


def _print_per_skill(results: list[dict]) -> None:
    """Print per-skill false-skip rates for the highest-concern threshold."""
    if not results:
        return
    # Show for the last (highest) threshold only — most informative for calibration
    worst = results[-1]
    psk = worst["per_skill_false_skip"]
    if not psk:
        return
    print(f"\nPer-skill false-skip rates at threshold={worst['threshold']:.2f}:")
    for skill, rate in sorted(psk.items(), key=lambda x: -x[1]):
        flag = " <-- EXCEEDS 5%" if rate > 0.05 else ""
        print(f"  {skill:<40} {rate * 100:>6.1f}%{flag}")


async def _main(args: argparse.Namespace) -> None:
    # Imports here so the pure summarize_skips can be imported without heavy deps
    from pathlib import Path as _Path

    from services.orchestrator.routing_pregate import SkillPreGate
    from services.skill_runner.skill_runner import SkillRunner

    thresholds = [float(t.strip()) for t in args.thresholds.split(",")]
    eval_path = _Path(args.eval)
    skills_dir = _Path(args.skills_dir)

    print(f"Loading eval cases from: {eval_path}")
    cases = _load_cases(eval_path)
    print(f"  {len(cases)} cases loaded")

    print(f"Discovering skills from: {skills_dir}")
    runner = SkillRunner(roots=[skills_dir])
    runner.discover()
    catalog = {name: meta.description for name, meta in runner.catalog.items()}
    print(f"  {len(catalog)} skills in catalog: {', '.join(sorted(catalog))}")

    # Build the real pre-gate (no threshold — we'll call max_similarity directly)
    gate = SkillPreGate(catalog, threshold=0.0)

    # Compute max_similarity per eval case
    rows: list[dict] = []
    print(f"\nComputing max_similarity for {len(cases)} eval cases...")
    for case in cases:
        task = case["task"]
        expected = case.get("expected", "none")
        sim = await gate.max_similarity(task)
        rows.append({"expected": expected, "max_sim": sim})

    # Add built-in no-match conversational probes
    print(f"Adding {len(_BUILTIN_NO_MATCH_PROBES)} built-in no-match probes...")
    for probe in _BUILTIN_NO_MATCH_PROBES:
        sim = await gate.max_similarity(probe)
        rows.append({"expected": "none", "max_sim": sim})

    # Sweep thresholds and print results
    results = summarize_skips(rows, thresholds=thresholds)
    _print_table(results)
    _print_per_skill(results)

    # Recommend a threshold
    rec = _recommend_threshold(results)
    print()
    if rec is not None:
        print(f"RECOMMENDED THRESHOLD: {rec:.2f}")
        print("  (highest threshold where every per-skill false-skip rate <= 5%)")
        print(f"  Set PREGATE_SIM_THRESHOLD={rec:.2f} and ENABLE_ROUTING_PREGATE=1")
    else:
        print("NO THRESHOLD MEETS THE 5% CRITERION — do NOT enable the pre-gate yet.")
        print("Consider improving catalog descriptions or lowering thresholds.")

    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sweep SkillPreGate similarity thresholds to measure false-skip rate."
    )
    parser.add_argument(
        "--eval",
        default="eval/routing_eval.jsonl",
        help="Path to routing eval JSONL file (default: eval/routing_eval.jsonl)",
    )
    parser.add_argument(
        "--skills-dir",
        default="services/skills",
        help="Path to skills directory (default: services/skills)",
    )
    parser.add_argument(
        "--thresholds",
        default="0.20,0.25,0.30,0.35,0.40",
        help="Comma-separated similarity thresholds to sweep (default: 0.20,0.25,0.30,0.35,0.40)",
    )
    args = parser.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
