# Routing A/B — broadened eval (N=5) — FINAL

Run to decide keep-vs-remove of the multi-intent decompose code, with bigger
cases + higher repeats. Opus judge (non-Gemma). Raw: ab_broad_big.json,
ab_broad_borderline.json. multi here is the Fix-11-fixed multi.

## Batch 1 — big multi-deliverable (the steel-man for multi), N=5
| | multi | single |
|--|------:|-------:|
| mean llm_calls | 24.33 | 9.00 |
| mean wall | 237s | 98s |
| outcome | complete 18/20, 2 TIMEOUTS | complete 20/20 |
Per-case quality (single/multi): email 0.6/0.6, stack 0.85/0.50 (multi 2/5 timeouts),
mixed 0.8/0.8, 3way 0.88/0.86. Judge: single non-inferior on big tasks; remove safe.

## Batch 2 — borderline categories, N=5
| | multi | single |
|--|------:|-------:|
| mean llm_calls | 14.5 | 7.05 |
| mean wall | 131s | 52.6s |
Quality (single/multi): compound 0.97/0.93 (multi 1/10 ok=False);
multi-unrelated 0.92/0.62 (multi drops BERT half 2/5); genuinely-ambiguous
0.94/0.92 — TIE, both clarify 5/5 (guardrail intact).

## Verdict: remove_multi (confidence: HIGH)
Single is non-inferior on quality in EVERY category incl. the ambiguity guardrail,
and multi shows no exclusive benefit anywhere while being 2-3x costlier and less
reliable (timeouts/errors). Consistent across 3 batches + wall-clock corroboration.

## Counter validity (a flat-9 sanity check)
The llm_calls counter is a real litellm success-callback; single takes 1/5/9/11
(not a constant) — 1=ambiguity halt, 5=direct-answer fast-path, 9=execute path,
11=execute+retry. The ~9 cluster is single's fixed one-goal pipeline shape; it is
orchestrator-side only (undercounts both modes, multi more). Wall-clock independently
confirms the latency gap.

## Caveats
- N=5 modest (directionally robust across 3 batches, not statistically tight).
- final_answer truncated at 500 chars in storage (equal across modes).
- single one-shots multi-part tasks rather than iterating; fine for all tested cases.
- multi remains a one-env-var fallback (ROUTING_MODE=multi) if removal is deferred.
