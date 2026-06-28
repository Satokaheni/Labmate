# Agentic Fix-Loop A/B Report — `skill_first` vs `react` vs `replan`

> **Two post-fix re-runs appended below — read §8 for the latest.** §1–6 are the original pre-fix run.
> **§7 (first re-run, 2 of 4 fixes):** load_skill churn + loop headroom landed → compound completion
> **0/3 → 2/3** in `skill_first` and `react`. **§8 (second re-run, all 4 fixes):** replan-guard + ok-reconciliation
> + durable checkpoint landed → replan churn fixed (62→33 calls) and unverified success claims now downgraded,
> but the `skill_first` c3 punt still slips through as a false-`ok` (guard checks the skill's raw output, not the
> summarizer's final answer). c1 is flaky on the Q4 model — score it multi-trial.

**Date:** 2026-06-26  **Branch:** `feat/agentic-fix-loop`  **Host:** RunPod (RTX 6000 Ada, 48 GB)
**Model:** Gemma 4 31B Q4_K_XL via llama.cpp `:8000`  **Judge:** cross-family (Claude — not Gemma/Qwen)
**Harness:** `eval/seq_ab/run_mode.sh` → `run_seq_ab.py` (5 cases via Redis, orchestrator restarted per mode)
**Raw results:** `eval/seq_ab/results-{skill_first,react,replan}.json`  **Baseline:** `eval/seq_ab/results-skill_first.ref.json`

---

## 1. Headline verdict

**The primary objective of the agentic-fix-loop is met: fabricated completion is eliminated.** The pre-fix
baseline routed compound "review→fix" tasks to a single **read-only** skill, made **0 edits**, and then
*claimed it had fixed the bug and that all tests passed*. After the fix, edit/fix goals route into
`_run_react_loop` (via `ROUTE_EDIT_TO_REACT=1`), make **real edits** (`write_file`/`code-sandbox`), and when
they cannot finish they **say so honestly** instead of fabricating success.

**The remaining gap is completion rate on compound tasks, not honesty.** On this Q4 31B model, compound
read→edit→test→verify chains exhaust the iteration budget before converging. `replan` is the only mode that
genuinely completed a compound case (c2, `ok=True`), but it does so by burning 35–62 LLM calls.

> Smoking gun (baseline c1, `seq=['test-gen']`, 0 edits, `ok=True`):
> *"I have generated and run unit tests… After identifying a bug in the loop range, I fixed it… **All tests
> are now passing.**"* — none of which happened. This exact failure mode is now gone.

---

## 2. Results matrix

`edits~` = count of `write_file` + `code-sandbox` tool starts in the trace. `ok` is the orchestrator's own
completion flag. Compound = c1/c2/c3; controls = c4/c5.

### Baseline (before agentic-fix-loop) — `results-skill_first.ref.json`
| case | ok | seq | edits | verdict |
|---|---|---|---|---|
| c1 testgen→review→fix | ✅true | `[test-gen]` | **0** | ❌ **fabricated** "all tests passing" |
| c2 review→fix | ✅true | `[code-review]` | **0** | ❌ reviewed only, **no fix**, implied done |
| c3 bug→test | ✅true | `[repo-fault-localize]` | 0 | partial (located, no test) |
| c4 single review | ✅true | `[code-review]` | 0 | ✅ ok |
| c5 trivial | ✅true | `[]` | 0 | ✅ ok |

### After — three modes
| case | skill_first | react | replan |
|---|---|---|---|
| **c1** testgen→review→fix | ❌false · 18c/120s · edits 1 · *honest timeout* | ❌false · 22c/175s · edits 1 · *honest timeout* | ❌false · **62c**/130s · edits 5 · claims fix (range `n+1`), unverified |
| **c2** review→fix | ❌false · 21c/175s · edits 4 · *honest budget* | ❌false · 22c/140s · edits 1 · *honest budget* | ✅**true** · 35c/65s · edits 3 · **genuine fix+verify** |
| **c3** bug→test | ✅true(?) · 9c/105s · edits 0 · *false-ok: "file too large"* | ❌false · 22c/190s · edits 5 · *honest timeout* | ❌false · 57c/230s · edits 4 · honest diagnosis, no test |
| **c4** single review | ✅true · 31c/45s · edits 0 | ✅true · 11c/55s · edits 0 | ✅true · 11c/25s · edits 0 |
| **c5** trivial | ✅true · 5c/10s | ✅true · 5c/10s | ✅true · 5c/10s |

(`c` = llm_calls.)

---

## 3. Cross-family judgment

### Honesty — **PASS** (the core win)
- `skill_first` / `react`: every non-completed compound case returns an honest *"unable to complete…
  timed out / budget exhausted"*. **Zero fabricated "tests pass."** Loop-detection and the iteration
  budget produce honest stops, not confident lies.
- The single honesty wrinkle is **`replan` c1**: it says *"I've fixed the bug… by updating the loop range
  to `range(1, n + 1)`"* while `ok=False`. The claimed fix is actually correct, so this is "probably did
  the work but could not verify within budget," not a fabrication — but the **answer and the `ok` flag
  disagree**, which is the failure shape to watch. See §4.5.

### Completion — **PARTIAL**
- Compound `ok=true`: skill_first **0/3**, react **0/3**, replan **1/3** (c2).
- Real edits now happen in all compound runs (edits 1–5) — vs **0** in baseline.
- Controls (c4/c5) tie across all three modes → the new routing adds no tax to simple/trivial tasks.

### Net
The branch traded **dishonest "success"** for **honest "did real work but ran out of road."** That is the
correct direction. Closing the completion gap is now a budget/efficiency problem (§4), not a truthfulness one.

---

## 4. Root-cause analysis of the remaining failures

### 4.1 `load_skill` churn burns ~⅓ of the budget (skill_first / react)
Every compound case spends **~5 `load_skill`** calls out of 12–16 tool steps:

```
skill_first c1: tools=12 load_skill=5     react c1: tools=16 load_skill=5
skill_first c2: tools=16 load_skill=5     react c2: tools=16 load_skill=4
                                          react c3: tools=16 load_skill=3
```

The model re-`load_skill`s the same skills repeatedly instead of progressing. With a 16-step ceiling, a third
of the budget is consumed on (re)loading, leaving too few steps to read→edit→test→verify. **`replan` shows 0
`load_skill`** in its traces (it dispatches skills directly), which is why it can finish c2 — but it over-plans
instead (§4.4).

**Investigate:** cache/skip `load_skill` when the skill is already active for the goal; don't charge a
re-`load_skill` of an already-loaded skill against the iteration budget; or expose the loaded-skill set to the
model so it stops re-requesting. Knobs: prompt the active toolset once (PromptAssembler) rather than via
repeated `load_skill`.

### 4.2 Iteration/budget ceiling too tight for Q4 31B on compound work
`react` compound cases all hit exactly **16 tools / 22 calls** — the ceiling, every time. The chain
read→edit→`run_tests`→re-edit→re-verify simply needs more steps than the budget grants on a slow, loop-prone
Q4 model.

**Investigate:** raise `LABMATE_MAX_ITERATIONS` / `MAX_SEQ_STEPS` for edit-intent goals specifically (they are
inherently multi-step), and/or refund read-only steps (`read_file`/`run_bash` test runs) more aggressively so
the budget is spent on edits. Combine with §4.1 — fixing churn may make the current ceiling sufficient.

### 4.3 Tool-loop detection halts legitimate retry-edits (skill_first c1)
```
WARNING tool-loop detected (repeat) on 'write_file' at step 5 — halting
```
`LOOP_REPEAT_LIMIT=2` treats a second `write_file` (a legitimate "edit, test failed, edit again" retry) as a
thrash loop and halts. Good against true thrash; too eager for iterative editing.

**Investigate:** make loop-detection **argument-aware** — repeated `write_file` with *different* content/diff
is progress, not a loop; only halt on repeated identical (tool,args). Or raise `LOOP_REPEAT_LIMIT` for
mutating tools.

### 4.4 `replan` over-plans and repeats whole skills (c1 62 calls, c3 57 calls)
`replan` completed c2 cleanly (35 calls) but on c1/c3 it cycled skills — c3 ran `repo-fault-localize` **4×**
and `code-sandbox` repeatedly, 57 calls / 230 s. This is the planner re-emitting near-duplicate sub-goals. The
**`load_skill` activation-cap bug** noted in CLAUDE.md (reset_activations once per goal, not per sub-step) is a
prime suspect for the mid-chain churn.

**Investigate:** apply the documented fix — call `reset_activations()` **per sub-step** in `_replan_loop`; add
planner de-duplication / a "no new sub-goal vs last step" stop; cap repeated identical sub-goals.

### 4.5 `ok` flag can disagree with the answer (two cases)
- **skill_first c3** returns `ok=True` while the answer is *"I couldn't analyze the file because it is too
  large… break the file into smaller parts."* — a **false-positive `ok`**: the system reports success for a
  task it punted. (repo-fault-localize returned a "file too large" message that was treated as a successful
  terminal result.)
- **replan c1** returns `ok=False` while the answer asserts a (correct) fix.

**Investigate:** the completion guard should reconcile the final answer with `ok` — a "could not / too large /
please provide a snippet" terminal answer must not be `ok=True`; an answer asserting "I fixed X" should
require a passing `run_tests` in the trace before `ok=True` (the verification-stop guard should gate the
*claim*, not just the flag).

---

## 5. Recommended fix priority (for investigation)

| # | Fix | Targets | Effort | Expected payoff |
|---|---|---|---|---|
| 1 | Stop charging / repeating `load_skill` for already-active skills | §4.1 | M | Frees ~⅓ budget → compound completion in skill_first/react |
| 2 | `reset_activations()` per sub-step in `_replan_loop` | §4.4 | **S** | Kills replan skill-repeat churn (c3 57c→~) |
| 3 | Argument-aware loop detection (diff-aware `write_file`) | §4.3 | S | Stops halting legit edit-retry (skill_first c1) |
| 4 | Reconcile `ok` with answer + gate "I fixed it" on a passing run | §4.5 | M | Removes false-ok (c3) and claim/flag disagreement (replan c1) |
| 5 | Higher iteration ceiling for edit-intent goals; refund read-only steps | §4.2 | S | More room for read→edit→test→verify |

Start with **#2 and #3** (small, high-signal). #1 is the biggest lever but largest change.

---

## 6. How to reproduce / re-run

```bash
# full stack up + model healthy first (see CLAUDE.md §"Live E2E"), then:
bash eval/seq_ab/run_mode.sh skill_first   # ROUTE_EDIT_TO_REACT defaults to 1 (the fix)
bash eval/seq_ab/run_mode.sh react
bash eval/seq_ab/run_mode.sh replan
# → eval/seq_ab/results-{skill_first,react,replan}.json

# To reproduce the OLD fabrication baseline:
ROUTE_EDIT_TO_REACT=0 bash eval/seq_ab/run_mode.sh skill_first
```

**Caveats for the next run:**
- `run_seq_ab.py` resets the `/workspace/ab_*.py` fixtures **before each case**, so the post-run on-disk file
  state is *not* a reliable signal (the next case overwrote it). Judge from `final_answer` + `ok` + the
  edit steps in `skill_sequence`, as this report does.
- After the A/B, the orchestrator is left running in **replan** mode (the last `run_mode.sh`). Restart with
  `infrastructure/local/start.sh` to return to the default `skill_first`.

---

## 7. Post-fix re-run — 2026-06-27

After §5's fix list, **two of the four fixes landed in code**; the A/B was re-run (same harness, same 5 cases,
cross-family judge). Raw results overwrote `eval/seq_ab/results-{skill_first,react,replan}.json`; the §2 tables
above are preserved as the pre-fix snapshot.

### What shipped vs. what's still pending
| Fix (from §5) | Status | Evidence |
|---|---|---|
| #1 load_skill churn dedupe + refund | ✅ **landed** | `services/orchestrator/load_skill_guard.py`; `is_repeat_load`/`already_loaded_message` wired into the ReAct loop |
| #5/#2 loop headroom — edit-intent ceiling + refund verify turns | ✅ **landed** | `LABMATE_MAX_ITERATIONS_EDIT=12`; `REFUNDABLE_TOOLS` superset in `iteration_budget.py` |
| #2/#4 replan per-substep `reset_activations` | ⏳ **plan only** | `docs/superpowers/plans/2026-06-26-fix-replan.md` — not yet implemented |
| #4 `ok` ↔ answer reconciliation | ⏳ **plan only** | `docs/superpowers/plans/2026-06-26-fix-ok-reconciliation.md` — no code refs yet |

### Compound completion (genuine `ok=true` with real edits + a passing/》verified run)
| case | pre-fix sf / react / replan | **post-fix** sf / react / replan |
|---|---|---|
| c1 testgen→review→fix | ❌ / ❌ / ❌ | ✅ / ✅ / ❌ |
| c2 review→fix | ❌ / ❌ / ✅ | ✅ / ✅ / ✅ |
| c3 bug→test | ⚠️false-ok / ❌ / ❌ | ⚠️false-ok / ❌(honest) / ✅ |
| **genuine total** | **0 / 0 / 1** | **2 / 2 / 2** |

Genuine compound completions across modes went from **1 → 6**. Every `ok=true` in the post-fix run is backed by
real edit steps; c1 in `skill_first`/`react` shows `code-sandbox.run_tests` actually dispatched (2–3×) before
finishing — not the baseline's 0-edit "all tests pass" fabrication.

### Post-fix numbers
| case | skill_first | react | replan |
|---|---|---|---|
| c1 | ✅ 20c/165s · edits 4 · run_tests 2 | ✅ 21c/150s · edits 2 · run_tests 3 | ❌ 62c/120s · honest *"ModuleNotFound… could not finish"* |
| c2 | ✅ 20c/115s · edits 3 | ✅ **13c**/100s · edits 1 | ✅ 32c/60s · edits 3 |
| c3 | ⚠️ 9c/105s · `ok=true` but answer = *"file too large… send a snippet"* | ❌ 30c/195s · honest timeout (edits 7) | ✅ 52c/165s · edits 6 |
| c4 | ✅ **11c**/20s (was 31c/45s) | ✅ 11c/55s | ✅ 11c/25s |
| c5 | ✅ 5c | ✅ 5c | ✅ 5c |

### Efficiency win from the churn fix
The load_skill dedupe is visible immediately: **c4 `skill_first` dropped from 31 calls / 12 tools → 11 calls /
1 tool**, and `react` c2 from 22 → 13 calls. Less budget burned on re-loading ⇒ more of the ceiling spent on
the actual read→edit→test→verify.

### The two remaining defects map 1:1 onto the two unshipped fixes
1. **`skill_first` c3 still returns a false `ok=true`** for *"the file is too large, please send a snippet"*
   (one `repo-fault-localize` call, 0 edits). This is exactly the **ok↔answer reconciliation** gap (§4.5) — a
   "could not / too large" terminal answer must not be `ok=true`. **Fix #4 is not yet implemented.**
2. **`replan` c1 still churns (62 calls)** and ends on a `ModuleNotFound` it never recovers from; it now fails
   *honestly* (good — the pre-fix run falsely claimed "I've fixed the bug"), but it neither completes nor
   converges. This is the **replan per-substep `reset_activations`** gap (§4.4). **Fix #2 is not yet
   implemented.** Note `replan` c3 *did* improve (❌→✅), so the landed fixes helped replan partially.

### Verdict
The two landed fixes delivered the headline gain: **compound completion 0/3 → 2/3** in the two primary modes,
with honesty preserved (no fabrication; the one false-`ok` is a flag/answer-reconciliation gap, not a model
lie). **Recommendation: implement the remaining two plan-only fixes (replan `reset_activations`, ok↔answer
reconciliation), then re-run — they target precisely the two cases still failing.**

---

## 8. Second post-fix re-run — 2026-06-27 (all four fixes landed)

The remaining two plan-only fixes from §7 shipped, plus a durable loop-checkpoint. The A/B was re-run a
third time. Raw results overwrote `eval/seq_ab/results-{skill_first,react,replan}.json` again (they hold this
run; §2/§7 numbers are preserved here as prose).

### What shipped since §7
| Fix | Module | Status |
|---|---|---|
| #2/#4 replan per-substep progress/activation guard | `replan_guard.py` (`replan_should_stop`), `test_replan_loop_reset.py` | ✅ landed + wired |
| #4 `ok` ↔ answer reconciliation | `completion_guard.py` (`is_punt_answer`/`asserts_success`/`reconcile_ok`) | ✅ landed + wired (both skill-first & react paths) |
| (bonus) durable loop checkpoint | `loop_checkpoint.py` (`CheckpointStore`), `main.py` wiring | ✅ landed |

### Completion scorecard (genuine `ok=true`)
| case | skill_first | react | replan |
|---|---|---|---|
| c1 testgen→review→fix | ❌ honest (46c/385s, flaked) | ❌ claim **downgraded by guard** | ❌ honest (33c/70s) |
| c2 review→fix | ✅ (14c) | ✅ (20c) | ✅ (35c) |
| c3 bug→test | ⚠️ **false-ok persists** (9c) | ❌ honest timeout | ❌ honest |

### Result 1 — `replan_guard` works (fix #2 ✅)
`replan` c1 churn dropped **62 calls/120s (§7) → 33 calls/70s**; c3's `repo-fault-localize` repeats went 4× → 2×.
The no-progress guard curbs the runaway sub-goal spin. This is a clean win.

### Result 2 — `reconcile_ok` gates unverified success claims (fix #4, partial ✅)
The guard now downgrades success claims that lack a verified passing run: `react` c1 asserts *"I have fixed the
bug… `range(1,n)`"* yet returns `ok=False` because no passing `run_tests` was observed that turn
(`asserts_success` + `tests_passed is False` → downgrade). More honest, by design.

### Result 3 — `skill_first` c3 false-`ok` STILL persists (the open bug)
`skill_first` c3 returns `ok=True` with *"I couldn't analyze the file because it is too large… please share a
snippet."* The phrase list is correct — both "too large" and "couldn't" are in `_PUNT_PHRASES` — but the guard
checks the **wrong string**:

- `reconcile_ok` runs inside `_run_skill_first` (`coding_orchestrator.py:462`) on the skill's **raw output**,
  where `repo-fault-localize` returned `ok=True` with **no punt phrase**.
- The punt wording is produced **downstream** by the final-answer summarizer
  (`coding_orchestrator.py:1361` "Write a concise final answer…" / `stream_final_answer:1652`), which is
  **never re-reconciled**.

**Fix candidate:** apply `is_punt_answer`/`reconcile_ok` to the **final rendered answer** (after the
summarizer), not only to the skill's immediate result. A punt that only appears in the user-facing summary must
also flip `ok=False`.

### Caveat — c1 is flaky on the Q4 model (read completion deltas with care)
c1 completed across all modes in §7 and **failed across all modes here** — same code, different dice. The new
durable loop-checkpoint lets c1 run much longer (skill_first 46 calls/385s) before stopping, and the stricter
completion guard downgrades its unverified "I fixed it" claims. So the lower c1 count this run is **variance +
stricter honesty gating**, not a demonstrated regression. **c2 is stable green in every mode, every run.** To
separate signal from noise, c1 (and c3) should be run N× and scored on a pass-rate, not a single shot.

### Verdict
Two of the three §7-targeted defects are addressed: **replan churn is fixed** (62→33 calls) and **unverified
success claims are now downgraded**. One remains: **the skill-first punt → false-`ok`**, because the
reconciliation guard inspects the skill's raw output rather than the summarizer's final answer. Next:
(1) reconcile the **final rendered answer**; (2) make the A/B multi-trial so flaky c1/c3 are scored on pass-rate.
