# Repeat-Analysis Guard — Design Spec

> Implementation plan: `docs/superpowers/plans/2026-07-03-repeat-analysis-guard.md`.
> This is a **harness-behavior** change (touches the ReAct loop), so it ships **flag-gated,
> default-OFF** and byte-identical when off. Whether it lifts pass-rate is a live A/B question.

**Goal:** Stop the ReAct loop from burning its iteration budget on **repeated read-only
analysis** (re-running `code-review` on a file it already reviewed) and instead steer the
model to make the edit. Convert wasted re-review turns into forward pressure toward the fix.

## Root cause (systematic-debugging Phase 1, confirmed from code + live traces)

On the 12B, seq_ab c2 ("Review `ab_buggy.py`, then fix") fails ~⅓ of the time by **churning**:
`code-review → code-sandbox → write_file → code-review → …`, re-reviewing the same file until
the `LABMATE_MAX_ITERATIONS_EDIT=12` budget exhausts, then honestly punting. Live traces (Q6
n=15: 11 pass / 4 fail): all failures are budget-exhaustion punts at 51/25/40/37 calls with
heavy re-review; passes converge (read→review→edit→verify→stop). **It's about convergence, not
call count** — some 43/47-call runs pass. Three loop guards that *should* curb the re-review
all miss it:

1. **`call_skill_tool` has no repeat guard** (`coding_orchestrator.py:1084`). `load_skill`
   dedups repeats (`:1025-1043`, via `load_skill_guard.py`); re-*calling* an already-loaded
   skill's tool (re-running the review) is never short-circuited. **Primary gap.**
2. **LoopDetector can't see it** — it keys on the outer `call_skill_tool` + full inner args
   (`loop_detection.py:51-62`), so any re-review with reworded args is a fresh signature; and
   all `call_skill_tool` share the looser `LOOP_REPEAT_LIMIT_MUTATING=4`.
3. **No-progress breaker counts churn as progress** — `_turn_made_progress`
   (`coding_orchestrator.py:502`) returns true on *any* tool call, so re-analysis never trips it.

Only the (non-refundable) budget curbs the churn → the honest punt. This is a fixable harness
gap, not pure model dice.

## The fix

A **repeat-analysis guard** mirroring `load_skill_guard.py`, but for *calls* to read-only
analysis skills. When the model calls a guarded analysis skill on a target it **already
analyzed this goal**, short-circuit the execution and return a steer:
*"You already ran `code-review` on `ab_buggy.py` this goal and found the issue — make the edit
with `write_file` and run the tests; don't re-review."*

### Scope decisions (locked; flagged for review)
- **Guarded skills** — `ANALYSIS_SKILLS = {"code-review", "critique", "design-critique"}`
  (read-only "produce a diagnosis" skills), overridable via `REPEAT_ANALYSIS_SKILLS`.
  **Deliberately NOT `code-sandbox` / `run_tests`** — re-running those *after an edit* is
  legitimate verification, not churn. The task literally says "review then fix," so the *first*
  review is allowed; only re-review is curbed.
- **Key granularity** — `analysis_key(skill, arguments)` = `skill` + a best-effort *target*
  pulled from common arg fields (`file`/`path`/`filename`/`target`); no target → key on skill
  alone. So re-reviewing the **same** file is caught; reviewing a **different** file is allowed.
- **Effect** — short-circuit (skip the skill run) + return the steer + **refund the turn**
  (`budget.refund()`, mirroring the `load_skill` dedup). **CORRECTED 2026-07-03 after the first
  A/B:** the original design consumed the turn (no refund), reasoning it kept a budget backstop.
  That was wrong — it defeats the guard's *purpose*: the whole point is to free an iteration for
  the actual edit, so consuming it means the churn still drains the budget and punts at the same
  cap (the A/B showed exactly this — guard fired, zero effect on loop turns). Refunding is what
  the `load_skill` precedent does and is safe (persistent re-review is bounded by the wall-clock
  deadline, and the steer redirects to the edit just as "already loaded" redirects to the tools).
- **Flag** — `ENABLE_REPEAT_ANALYSIS_GUARD` default `"0"` (**OFF**). Off ⇒ byte-identical to
  today. Measure via whole-suite A/B (Q4) before any default flip.
- **State** — loop-local `seen_analysis: set[str]`, **not** checkpointed (best-effort; a
  crash-resume that resets it merely allows one extra review — acceptable, keeps surface small).

### Components
- **New:** `services/orchestrator/repeat_analysis_guard.py` — pure helpers:
  `ANALYSIS_SKILLS`, `analysis_skills()` (env), `repeat_analysis_guard_enabled()` (env),
  `analysis_key(skill, arguments) -> str`, `is_guarded_analysis(skill) -> bool`,
  `build_analysis_steer(skill, key) -> dict`.
- **Modify:** `coding_orchestrator.py` — module flag constant near `:78`; init
  `seen_analysis: set[str] = set()` near `:604`; wrap the existing `call_skill_tool` body
  (`:1084-1140`) in an `else`, short-circuiting a guarded repeat before `skill_router.execute`;
  emit `analysis.deduped`.
- **Tests:** unit tests for the pure helpers; a BDD/loop test that a repeat guarded call is
  short-circuited when the flag is ON and executes normally when OFF (default).
- **Docs:** CLAUDE.md flag row.

## Non-goals / constraints
- Default-OFF, behavior-preserving. No change to routing, budget sizing, or other guards.
- Does NOT touch `code-sandbox`/`run_tests` re-runs (legit verification).
- Whether it lifts c2 pass-rate is unproven — it may instead make failures *faster/cheaper*
  (punt at ~15 calls, not 47). Both are acceptable; the A/B decides. Must not regress c1/c3/c4/c6.
- Follows the repo Implementation Workflow (subagent build → Opus review) + whole-suite A/B.
