"""Sequencing A/B harness: run a fixed case set against the live orchestrator and
record, per case, the skill sequence + ok + llm_calls + wall-time.

The orchestrator's SEQUENCING_MODE is process-wide (read at import), so this script
is invoked ONCE PER MODE after the orchestrator is (re)started with that env var.
Results are written to eval/seq_ab/results-<mode>.json for later comparison/judging.
"""

import json
import os
import sys
import time

import redis

from eval.metric_meaning import seq_ab_meaning_block
from eval.seq_ab.local_tool_responder import LocalToolResponder

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
MODE = sys.argv[1] if len(sys.argv) > 1 else "unknown"
OUT = f"eval/seq_ab/results-{MODE}.json"
TRIALS = int(
    os.getenv("TRIALS", "3")
)  # run each case N times; pass-rate scoring. TRIALS=1 == single-shot.

# Fixtures are reset before each case so a prior mode's fix doesn't leak.
FIXTURES = {
    "/workspace/ab_factorial.py": (
        'def factorial(n):\n    """Return n! (product of 1..n). 0! == 1."""\n'
        "    result = 1\n    for i in range(1, n):          # bug: excludes n\n"
        "        result *= i\n    return result\n"
    ),
    "/workspace/ab_buggy.py": (
        'def average(nums):\n    """Return the arithmetic mean of a list of numbers."""\n'
        "    total = 0\n    for x in nums:\n        total += x\n"
        "    return total / len(nums) + 1    # bug: stray + 1\n"
    ),
    "/workspace/ab_off.py": (
        'def last_index(seq, target):\n    """Return the index of the LAST occurrence of target, or -1 if absent."""\n'
        "    for i in range(len(seq)):\n        if seq[i] == target:\n"
        "            return i               # bug: returns FIRST match, not last\n    return -1\n"
    ),
}

CASES = [
    {
        "id": "c1_testgen_review_fix",
        "kind": "compound",
        "task": "Generate unit tests for the factorial function in /workspace/ab_factorial.py, run them, find and fix the bug, and re-run until they pass.",
    },
    {
        "id": "c2_review_fix",
        "kind": "compound",
        "task": "Review /workspace/ab_buggy.py for bugs, then fix the code.",
    },
    {
        "id": "c3_bug_then_test",
        "kind": "compound",
        "task": "Find the bug in /workspace/ab_off.py and write a unit test that exposes it.",
    },
    {
        "id": "c4_single_review",
        "kind": "control_single",
        "task": "Find bugs in /workspace/ab_buggy.py.",
    },
    {"id": "c5_trivial", "kind": "control_trivial", "task": "What is 2+2? Reply in one sentence."},
]


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


def reset_fixtures():
    for path, body in FIXTURES.items():
        with open(path, "w") as f:
            f.write(body)


def run_case(r, case):
    reset_fixtures()
    task_id = f"ab-{MODE}-{case['id']}-{int(time.time())}"
    workspace = os.getenv("WORKSPACE_PATH", "/workspace")
    responder = LocalToolResponder(r, task_id, workspace)
    responder.start()
    try:
        payload = json.dumps({"task_id": task_id, "task": case["task"], "session_id": task_id})
        t0 = time.time()
        r.xadd("labmate:goals", {"payload": payload})
        result = None
        for _ in range(240):  # up to ~20 min hard cap
            v = r.get(f"labmate:result:{task_id}")
            if v:
                result = json.loads(v)
                break
            time.sleep(5)
        elapsed = time.time() - t0
        # Collect the skill/tool sequence from the event stream
        seq = []
        entries = r.xrange(f"labmate:events:{task_id}")
        for _id, fields in entries:
            raw = fields.get("event")
            if not raw:
                continue
            ev = json.loads(raw)
            if ev.get("type") == "tool.start":
                seq.append(ev.get("name", "?"))
        state = (result or {}).get("state", {})
        return {
            "id": case["id"],
            "kind": case["kind"],
            "task": case["task"],
            "ok": (result or {}).get("ok"),
            "skill_sequence": seq,
            "llm_calls": (result or {}).get("llm_calls"),
            "wall_s": round(elapsed, 1),
            "final_answer": (state.get("final_answer") or "")[:1200],
            "task_id": task_id,
        }
    finally:
        responder.stop()


def main():
    r = redis.from_url(REDIS_URL, decode_responses=True)
    # Optional case filter: AB_ONLY=c1,c3 runs only cases whose id contains any of
    # the comma-separated substrings. Unset = all cases (back-compat). A filtered
    # run writes to a distinct file so it never clobbers the full results-<mode>.json.
    only = [s.strip() for s in os.getenv("AB_ONLY", "").split(",") if s.strip()]
    cases = [c for c in CASES if not only or any(s in c["id"] for s in only)]
    out_path = OUT
    if only:
        tag = "-".join(only).replace("/", "_")
        out_path = f"eval/seq_ab/results-{MODE}-only-{tag}.json"
        print(f"[{MODE}] AB_ONLY={only} -> {len(cases)} case(s) -> {out_path}", flush=True)
    out = {"mode": MODE, "trials": TRIALS, "metric_meaning": seq_ab_meaning_block(), "cases": []}
    for case in cases:
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
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    # Final pass-rate roll-up across all cases (quick read).
    print(f"[{MODE}] pass-rate summary:", flush=True)
    for c in out["cases"]:
        print("  " + summarize_line(MODE, c["id"], c), flush=True)
    print(f"[{MODE}] wrote {out_path}", flush=True)


if __name__ == "__main__":
    main()
