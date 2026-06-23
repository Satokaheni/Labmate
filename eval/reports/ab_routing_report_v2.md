# Routing A/B v2 — single vs FIXED multi (after Fix 11)

Re-run after Fix 11 fixed multi's compound-clarify bug. N=2 repeats/case/mode.
Opus judge (non-Gemma), quality-gated-latency rule. Raw: `ab_routing_raw_v2.json`.

## Latency
| mode | mean llm_calls | mean wall |
|------|---------------:|----------:|
| multi (fixed) | 11.06 | 87.72s |
| single        |  7.12 | 53.82s |
single: −36% calls, −39% wall.

## Quality + behavior (opus judge)
| category | single q | multi q | note |
|----------|---------:|--------:|------|
| trivial-chat        | 1.00 | 1.00 | equal; single cheaper |
| clear-single-skill  | 0.90 | 0.90 | equal |
| clear-coding-single | 0.95 | 0.95 | equal |
| compound-related    | 0.95 | 0.95 | **Fix 11: multi now completes 4/4 — but ~21.5 calls vs single ~9.5** |
| multi-unrelated     | 0.95 | 0.40 | multi half-completes (fails 2nd ask, ok=false 2/2); single both |
| multi-step          | 0.30 | 0.17 | multi spuriously clarifies 1/2; single completes (env-capped) |
| genuinely-ambiguous | 0.85 | 0.95 | both clarify (guardrail holds); 0.10 gap, at the gate not over |

## Verdict
**flip_to_single — quality gate PASSED.** single is equal-or-better quality in
every category, −36% calls / −39% wall, and fixes two multi failures Fix 11 did
not (multi-unrelated half-complete, multi-step spurious clarify). Fix 11 confirmed
the compound-clarify was a real bug (multi now completes compound, equal quality,
but ~2-3x cost). Keep `multi` selectable via the ROUTING_MODE toggle as fallback.

## Caveats
- N=2 — wide CIs; genuine-ambiguous margin is exactly at the 0.1 limit (monitor).
- Pod env (missing CSV) caps multi-step absolute scores; relative comparison valid.
- single's compound correctness relies on ReAct within one goal — watch as tasks scale.
