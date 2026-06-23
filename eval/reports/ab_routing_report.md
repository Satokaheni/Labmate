# Routing A/B: single-intent vs multi-intent

Branch `feat/agent-event-stream`. Harness `eval/ab_routing.py`, corpus
`eval/ab_routing.jsonl` (8 cases), N=2 repeats/case/mode. Judge: opus (non-Gemma),
quality-gated-latency rule. Raw: `eval/reports/ab_routing_raw.json`.

## Latency (deterministic)
| mode | mean llm_calls | mean wall |
|------|---------------:|----------:|
| multi  | 7.94 | 59.73s |
| single | 6.50 | 46.35s |
single: −18% calls, −22% wall.

## Quality + behavior (opus judge)
| category | single q | multi q | note |
|----------|---------:|--------:|------|
| trivial-chat        | 1.00 | 1.00 | both answer; single cheaper |
| clear-coding-single | 1.00 | 1.00 | both deliver code |
| clear-single-skill  | 0.90 | 0.90 | both route to dataset-search |
| compound-related    | 0.93 | 0.05 | **multi clarifies 4/4, delivers nothing; single delivers function+test** |
| multi-unrelated     | 0.93 | 0.65 | single both deliverables (9 calls); multi 1/2, one ok=false (~17 calls) |
| genuinely-ambiguous | 0.92 | 0.91 | both clarify — guardrail intact |
| multi-step          | 0.12 | 0.12 | both fail — environmental (missing results.csv on this pod) |

## Verdict
**flip_to_single — quality gate PASSED.** single non-inferior on quality in every
category, decisively better on compound (multi is actively broken there), faster
and cheaper overall, with the ambiguity guardrail intact. Keep `multi` reachable
via the `ROUTING_MODE` toggle as a fallback.

## Caveats
- N=2 repeats — exposes multi's compound defect but thin for borderline quality.
- Pod skill failures confound multi-step (both arms) and skill wall-times.
- multi's compound-clarify is likely a fixable routing bug; if patched, gap narrows.
- single compound correctness depends on ReAct delivering all parts — monitor in prod.
