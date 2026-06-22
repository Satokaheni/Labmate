#!/usr/bin/env python3
"""
A/B routing live harness — compares ARM A ("multi") vs ARM B ("single").

This script DOES NOT call the model. It only drives Redis: for each labeled case
in eval/ab_routing.jsonl, across repeats and modes, it XADDs a goal payload to the
"labmate:goals" stream (with a per-request routing_mode override) and polls
GET labmate:result:<task_id> until the running orchestrator writes the result.

  ARM A "multi"  (default): route() decomposes the task into N sub-intents and
                            confidence-checks each (a sequential goal chain).
  ARM B "single":           the whole message is treated as ONE intent
                            (decompose() is skipped, sub_intents=[task]); ReAct
                            sequences any sub-steps within the single goal.

Per (mode, case, repeat) it records a row with the result's llm_calls counter,
wall-clock seconds, and the observed behavior signals (ok, awaiting_clarification,
direct_answer, final_answer). All rows are written to
eval/reports/ab_routing_raw.json and a compact aggregate table is printed.

Run the full stack first (serve-model.sh / start.sh / status.sh), then:

    python eval/ab_routing.py --repeats 2 --timeout 240
"""
from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from pathlib import Path

import redis  # sync client is fine — this is a driver, not a hot path


GOALS_STREAM = "labmate:goals"
RESULT_PREFIX = "labmate:result:"

_HERE = Path(__file__).resolve().parent
DEFAULT_JSONL = _HERE / "ab_routing.jsonl"
DEFAULT_OUT = _HERE / "reports" / "ab_routing_raw.json"


def load_cases(path: Path) -> list[dict]:
    cases: list[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            cases.append(json.loads(line))
    return cases


def submit_and_wait(
    r: "redis.Redis", task: str, mode: str, timeout: float
) -> tuple[dict | None, float, bool]:
    """XADD a goal with routing_mode=mode, poll for the result.

    Returns (result_dict_or_None, wall_clock_s, timed_out).
    """
    task_id = f"ab-{mode}-{uuid.uuid4().hex[:12]}"
    payload = {
        "task_id": task_id,
        "task": task,
        "session_id": task_id,
        "routing_mode": mode,
    }
    key = f"{RESULT_PREFIX}{task_id}"
    start = time.monotonic()
    r.xadd(GOALS_STREAM, {"payload": json.dumps(payload)})
    while True:
        val = r.get(key)
        if val is not None:
            wall = time.monotonic() - start
            try:
                return json.loads(val), wall, False
            except json.JSONDecodeError:
                return {"ok": False, "error": "malformed_result_json"}, wall, False
        if time.monotonic() - start > timeout:
            return None, time.monotonic() - start, True
        time.sleep(0.5)


def row_from_result(
    mode: str, case: dict, repeat: int, result: dict | None, wall: float, timed_out: bool
) -> dict:
    state = (result or {}).get("state") or {}
    final_answer = state.get("final_answer", "") if isinstance(state, dict) else ""
    return {
        "mode": mode,
        "id": case["id"],
        "category": case.get("category", ""),
        "repeat": repeat,
        "llm_calls": (result or {}).get("llm_calls"),
        "wall_clock_s": round(wall, 2),
        "ok": bool((result or {}).get("ok")) if result is not None else False,
        "awaiting_clarification": bool(state.get("awaiting_clarification")) if isinstance(state, dict) else False,
        "direct_answer": bool(state.get("direct_answer")) if isinstance(state, dict) else False,
        "final_answer": (final_answer or "")[:500],
        "timed_out": timed_out,
    }


def _mean(values: list) -> float | None:
    nums = [v for v in values if isinstance(v, (int, float))]
    if not nums:
        return None
    return sum(nums) / len(nums)


def observed_behavior(row: dict) -> str:
    if row["timed_out"]:
        return "timeout"
    if not row["ok"]:
        return "error"
    if row["awaiting_clarification"]:
        return "clarify"
    if row["direct_answer"]:
        return "answer"
    return "complete"


def print_aggregate(rows: list[dict], modes: list[str]) -> None:
    print("\n=== A/B routing aggregate ===")
    # Per-mode means.
    print("\nPer mode:")
    print(f"{'mode':<10} {'n':>4} {'mean_llm_calls':>16} {'mean_wall_s':>14}")
    for mode in modes:
        mrows = [r for r in rows if r["mode"] == mode]
        if not mrows:
            continue
        ml = _mean([r["llm_calls"] for r in mrows])
        mw = _mean([r["wall_clock_s"] for r in mrows])
        ml_s = f"{ml:.2f}" if ml is not None else "n/a"
        mw_s = f"{mw:.2f}" if mw is not None else "n/a"
        print(f"{mode:<10} {len(mrows):>4} {ml_s:>16} {mw_s:>14}")

    # Per (mode, category) observed behavior tallies.
    print("\nPer (mode, category) observed behavior:")
    cats = sorted({r["category"] for r in rows})
    for mode in modes:
        for cat in cats:
            sub = [r for r in rows if r["mode"] == mode and r["category"] == cat]
            if not sub:
                continue
            from collections import Counter
            beh = Counter(observed_behavior(r) for r in sub)
            beh_s = ", ".join(f"{k}={v}" for k, v in sorted(beh.items()))
            ml = _mean([r["llm_calls"] for r in sub])
            ml_s = f"{ml:.1f}" if ml is not None else "n/a"
            print(f"  {mode:<8} {cat:<22} calls~{ml_s:<6} {beh_s}")


def main() -> None:
    ap = argparse.ArgumentParser(description="A/B routing live harness (multi vs single).")
    ap.add_argument("--repeats", type=int, default=2, help="repeats per (case, mode)")
    ap.add_argument("--timeout", type=float, default=240.0, help="per-task result timeout (s)")
    ap.add_argument("--modes", default="multi,single", help="comma-separated routing modes")
    ap.add_argument("--jsonl", default=str(DEFAULT_JSONL), help="labeled corpus path")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="raw rows output JSON path")
    args = ap.parse_args()

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    r = redis.from_url(redis_url, decode_responses=True)

    cases = load_cases(Path(args.jsonl))
    rows: list[dict] = []
    total = len(cases) * args.repeats * len(modes)
    done = 0
    for case in cases:
        for repeat in range(args.repeats):
            for mode in modes:
                done += 1
                print(
                    f"[{done}/{total}] mode={mode} id={case['id']} repeat={repeat} ...",
                    flush=True,
                )
                try:
                    result, wall, timed_out = submit_and_wait(
                        r, case["task"], mode, args.timeout
                    )
                except Exception as exc:  # resilient: never let one case kill the run
                    print(f"    ERROR: {exc}", flush=True)
                    result, wall, timed_out = None, 0.0, True
                row = row_from_result(mode, case, repeat, result, wall, timed_out)
                rows.append(row)
                print(
                    f"    -> {observed_behavior(row)} "
                    f"llm_calls={row['llm_calls']} wall={row['wall_clock_s']}s",
                    flush=True,
                )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, default=str)
    print(f"\nWrote {len(rows)} rows to {out_path}")

    print_aggregate(rows, modes)


if __name__ == "__main__":
    main()
