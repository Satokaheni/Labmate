"""Sequencing A/B harness: run a fixed case set against the live orchestrator and
record, per case, the skill sequence + ok + llm_calls + wall-time.

The orchestrator's SEQUENCING_MODE is process-wide (read at import), so this script
is invoked ONCE PER MODE after the orchestrator is (re)started with that env var.
Results are written to eval/seq_ab/results-<mode>.json for later comparison/judging.
"""
import json, sys, time, os
import redis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
MODE = sys.argv[1] if len(sys.argv) > 1 else "unknown"
OUT = f"eval/seq_ab/results-{MODE}.json"

# Fixtures are reset before each case so a prior mode's fix doesn't leak.
FIXTURES = {
    "/workspace/ab_factorial.py": (
        'def factorial(n):\n    """Return n! (product of 1..n). 0! == 1."""\n'
        '    result = 1\n    for i in range(1, n):          # bug: excludes n\n'
        '        result *= i\n    return result\n'
    ),
    "/workspace/ab_buggy.py": (
        'def average(nums):\n    """Return the arithmetic mean of a list of numbers."""\n'
        '    total = 0\n    for x in nums:\n        total += x\n'
        '    return total / len(nums) + 1    # bug: stray + 1\n'
    ),
    "/workspace/ab_off.py": (
        'def last_index(seq, target):\n    """Return the index of the LAST occurrence of target, or -1 if absent."""\n'
        '    for i in range(len(seq)):\n        if seq[i] == target:\n'
        '            return i               # bug: returns FIRST match, not last\n    return -1\n'
    ),
}

CASES = [
    {"id": "c1_testgen_review_fix", "kind": "compound",
     "task": "Generate unit tests for the factorial function in /workspace/ab_factorial.py, run them, find and fix the bug, and re-run until they pass."},
    {"id": "c2_review_fix", "kind": "compound",
     "task": "Review /workspace/ab_buggy.py for bugs, then fix the code."},
    {"id": "c3_bug_then_test", "kind": "compound",
     "task": "Find the bug in /workspace/ab_off.py and write a unit test that exposes it."},
    {"id": "c4_single_review", "kind": "control_single",
     "task": "Find bugs in /workspace/ab_buggy.py."},
    {"id": "c5_trivial", "kind": "control_trivial",
     "task": "What is 2+2? Reply in one sentence."},
]

def reset_fixtures():
    for path, body in FIXTURES.items():
        with open(path, "w") as f:
            f.write(body)

def run_case(r, case):
    reset_fixtures()
    task_id = f"ab-{MODE}-{case['id']}-{int(time.time())}"
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
        "id": case["id"], "kind": case["kind"], "task": case["task"],
        "ok": (result or {}).get("ok"),
        "skill_sequence": seq,
        "llm_calls": (result or {}).get("llm_calls"),
        "wall_s": round(elapsed, 1),
        "final_answer": (state.get("final_answer") or "")[:1200],
        "task_id": task_id,
    }

def main():
    r = redis.from_url(REDIS_URL, decode_responses=True)
    out = {"mode": MODE, "cases": []}
    for case in CASES:
        print(f"[{MODE}] running {case['id']} ...", flush=True)
        res = run_case(r, case)
        print(f"    ok={res['ok']} seq={res['skill_sequence']} calls={res['llm_calls']} {res['wall_s']}s", flush=True)
        out["cases"].append(res)
    os.makedirs("eval/seq_ab", exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(out, f, indent=2)
    print(f"[{MODE}] wrote {OUT}", flush=True)

if __name__ == "__main__":
    main()
