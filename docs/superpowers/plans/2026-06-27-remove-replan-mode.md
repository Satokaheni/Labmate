# Remove the `replan` Sequencing Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete the `replan` sequencing mode and its entire subsystem. `skill_first` becomes the sole production default; `react` remains an opt-in mode (`SEQUENCING_MODE=react`). `skill_first` and `react` must be completely unaffected.

**Architecture:** `react_execute` dispatches on `SEQUENCING_MODE`. After this change there are exactly two values: `skill_first` (default) and `react` (opt-in). The `replan` branch and its planner-driven continuation loop (`_replan_loop`), its compound-gate classifier (`_is_compound`), the no-progress guard module (`replan_guard.py`), the `MAX_SEQ_STEPS` / `REPLAN_*` env knobs, and all replan tests + eval artifacts are removed. The shared `_run_react_loop` and `_run_skill_first` (used by both kept modes) are untouched, and the per-goal `reset_activations()` call at the top of `react_execute` is **kept** (it is general, not replan-specific).

**Tech Stack:** Python 3.11 (orchestrator), pytest + pytest-asyncio, bash (eval harness).

## Global Constraints

- Behavior of `skill_first` and `react` MUST be byte-for-byte preserved. The ONLY dispatch change is removing the `if SEQUENCING_MODE == "replan": return await self._replan_loop(goal)` branch.
- Keep the per-goal `self.skill_router.runner.reset_activations()` call at the top of `react_execute` (the one guarded by `if self.skill_router is not None:` BEFORE the dispatch) — it resets the activation budget for every goal in every mode. Only the SECOND `reset_activations()` call (inside `_replan_loop`) is removed (with the method).
- Do NOT delete historical records: dated plan docs under `docs/superpowers/plans/2026-06-26-*.md` (incl. `2026-06-26-fix-replan.md`) and `eval/reports/*.md` stay as-is (they document past work). Only LIVE guidance (CLAUDE.md) is updated.
- stdout-sacred / no tiktoken / Chroma client-server — unchanged.
- After the change, `grep -rn "replan\|_is_compound\|MAX_SEQ_STEPS\|REPLAN_" services/ tests/ eval/seq_ab/*.sh eval/seq_ab/*.py` returns NOTHING (code/eval-harness clean; docs/reports may still mention it historically).

---

### Task 1: Remove the replan subsystem from the orchestrator

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py`
- Delete: `services/orchestrator/replan_guard.py`

- [ ] **Step 1: Delete the replan_guard module + its import**

Delete the file:

```bash
git rm services/orchestrator/replan_guard.py
```

In `services/orchestrator/coding_orchestrator.py`, remove the import line:

```python
from .replan_guard import replan_should_stop
```

- [ ] **Step 2: Remove the replan env knobs**

In `coding_orchestrator.py`, near the top module-level config (around the `SEQUENCING_MODE = os.getenv(...)` block), remove these three knobs and their comment lines (keep `SEQUENCING_MODE` itself):

```python
MAX_SEQ_STEPS = int(os.getenv("MAX_SEQ_STEPS", "5"))
# ... comment ...
REPLAN_COMPOUND_GATE = os.getenv("REPLAN_COMPOUND_GATE", "1") == "1"
# ... comment ...
REPLAN_MAX_SKILL_REPEATS = int(os.getenv("REPLAN_MAX_SKILL_REPEATS", "2"))
```

Also trim the `SEQUENCING_MODE` doc comment block so it no longer describes a `replan` mode (it should describe only `skill_first` (default) and `react` (opt-in)).

- [ ] **Step 3: Remove the replan dispatch branch**

In `react_execute`, delete ONLY these two lines (and fold the now-stale comment):

```python
        if SEQUENCING_MODE == "replan":
            return await self._replan_loop(goal)
```

Update the dispatch comment block just above it to drop the `replan` bullet (keep the `react` and `skill_first` bullets). The remaining dispatch logic is unchanged:

```python
        if SEQUENCING_MODE != "react" and requires_editing(goal):
            return await self._run_react_loop(goal, self.max_steps)
        if SEQUENCING_MODE != "react":
            skilled = await self._run_skill_first(goal)
            if skilled is not None:
                return skilled
        return await self._run_react_loop(goal, self.max_steps)
```

> KEEP the `reset_activations()` block that sits ABOVE this dispatch (the per-goal reset) — it is not replan-specific.

- [ ] **Step 4: Delete the `_is_compound` and `_replan_loop` methods**

Delete both methods in full from `coding_orchestrator.py`:
- `async def _is_compound(self, goal: str) -> bool:` (the cheap multi-step classifier)
- `async def _replan_loop(self, goal: str) -> dict:` (the planner-driven continuation loop)

They are contiguous and used ONLY by replan (`_is_compound` is called only inside `_replan_loop`; `_replan_loop` is called only from the deleted dispatch branch). Delete from the start of `_is_compound` through the end of `_replan_loop`, up to (not including) the next method (`async def _run_worker`).

- [ ] **Step 5: Verify imports + dispatch integrity**

Run:

```bash
grep -nE "replan|_replan_loop|_is_compound|MAX_SEQ_STEPS|REPLAN_|replan_should_stop" services/orchestrator/coding_orchestrator.py
```
Expected: NO output.

```bash
python -c "import services.orchestrator.coding_orchestrator as m; print('import OK')"
```
Expected: `import OK` (no ImportError from the removed `replan_guard` import).

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/coding_orchestrator.py services/orchestrator/replan_guard.py
git commit -m "refactor(orchestrator): remove the replan sequencing mode + subsystem"
```

---

### Task 2: Remove replan tests + eval artifacts

**Files:**
- Delete: `tests/services/orchestrator/test_replan_guard.py`, `tests/services/orchestrator/test_replan_loop_reset.py`, `tests/services/orchestrator/test_replan_progress_guard_bdd.py`, `tests/services/orchestrator/features/replan_progress_guard.feature`
- Modify: `tests/services/orchestrator/test_coding_orchestrator.py`
- Modify: `eval/seq_ab/run_mode.sh`
- Delete: `eval/seq_ab/results-replan.json`, `eval/seq_ab/results-replan-only-c1.json`

- [ ] **Step 1: Delete the replan-only test files + result artifacts**

```bash
git rm tests/services/orchestrator/test_replan_guard.py \
       tests/services/orchestrator/test_replan_loop_reset.py \
       tests/services/orchestrator/test_replan_progress_guard_bdd.py \
       tests/services/orchestrator/features/replan_progress_guard.feature \
       eval/seq_ab/results-replan.json \
       eval/seq_ab/results-replan-only-c1.json
```

- [ ] **Step 2: Remove the replan test class from test_coding_orchestrator.py**

In `tests/services/orchestrator/test_coding_orchestrator.py`, delete the entire test class that targets replan — it contains `test_replan_dispatches_to_replan_loop`, the compound-gate tests, and the `_is_compound` tests (`test_is_compound_defaults_true_on_parse_failure`, `test_is_compound_reads_bool`). Remove the whole `class Test...:` block (the one whose docstring says "Tests for the replan dispatch + compound gate (SEQUENCING_MODE=replan)").

Also fix the now-stale module docstring near the top (it currently says the default is `replan`):

```python
# BEFORE (stale):
#   default is now ``replan`` (planner-driven), so pin the dispatcher to ``skill_first``
#   here. Tests that specifically target ``replan``/``react`` set the mode themselves.
# AFTER:
#   default is ``skill_first``; tests that specifically target ``react`` set the mode themselves.
```

- [ ] **Step 3: Update the A/B runner usage**

In `eval/seq_ab/run_mode.sh`, change the usage line and any mode validation from `<skill_first|react|replan>` to `<skill_first|react>`. If the script validates/branches on a `replan` value, remove that branch.

- [ ] **Step 4: Verify the suite is clean**

```bash
grep -rnE "replan|_is_compound|MAX_SEQ_STEPS|REPLAN_" tests/ eval/seq_ab/*.sh eval/seq_ab/*.py
```
Expected: NO output.

```bash
PYTHONPATH=. python -m pytest tests/services/orchestrator -q
```
Expected: PASS, no collection errors (the deleted tests are gone; the kept skill_first/react tests pass).

- [ ] **Step 5: Commit**

```bash
git add tests/services/orchestrator/ eval/seq_ab/run_mode.sh
git commit -m "test(orchestrator): remove replan tests + eval artifacts"
```

---

### Task 3: Update CLAUDE.md — skill_first sole default, react opt-in, no replan

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Remove replan from the live guidance**

In `CLAUDE.md`, update the sequencing/latency + A/B sections so they describe exactly two modes:
- `skill_first` — **the sole default** (single-skill fast path for non-edit goals; edit-intent goals route to `_run_react_loop` via `ROUTE_EDIT_TO_REACT`).
- `react` — **opt-in** (`SEQUENCING_MODE=react`): always runs `_run_react_loop`; kept as a diagnostic / routing-regression baseline.

Specifically:
- Remove the `_replan_loop` / replan bullets and the "Replan activation-cap bug" notes from the harness/sequencing prose.
- Remove `MAX_SEQ_STEPS`, `REPLAN_COMPOUND_GATE`, `REPLAN_MAX_SKILL_REPEATS` from any "knobs" lists.
- In the A/B section (§9), change `run_mode.sh <skill_first|react|replan>` to `<skill_first|react>` and drop the replan rows/paragraphs; keep the c1/c2/c3 cases and the skill_first-vs-react framing.
- Add one line stating replan was removed (so future readers know it's intentional, not missing): e.g. "The `replan` mode was removed (2026-06-27) — it underperformed and added a planner subsystem; `skill_first`/`react` cover all needs."

Do NOT touch the dated plan docs under `docs/superpowers/plans/` or `eval/reports/` — those are historical records.

- [ ] **Step 2: Verify**

```bash
grep -nE "replan|REPLAN|MAX_SEQ_STEPS|_replan_loop" CLAUDE.md
```
Expected: at most the single intentional "replan was removed (2026-06-27)" note; no live instructions referencing a replan mode or its knobs.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(CLAUDE.md): skill_first sole default, react opt-in, replan removed"
```

---

### Task 4: Whole-suite regression gate

- [ ] **Step 1:** Run: `PYTHONPATH=. python -m pytest tests/ -q 2>&1 | tail -5` — Expected: full suite green (no NEW failures vs the `c2508f9` baseline), no collection errors, no `import replan_guard` errors.
- [ ] **Step 2:** Confirm both kept modes still dispatch: `grep -nE "SEQUENCING_MODE != \"react\"|SEQUENCING_MODE == \"replan\"" services/orchestrator/coding_orchestrator.py` — Expected: the `!= "react"` checks remain; NO `== "replan"` branch.

---

## Self-Review

- **Spec coverage:** orchestrator removal (Task 1) → tests/eval cleanup (Task 2) → docs (Task 3) → regression gate (Task 4). ✓
- **No collateral:** `_run_react_loop`, `_run_skill_first`, and the top-of-`react_execute` `reset_activations()` are untouched; only the replan dispatch branch + replan-exclusive methods/module/knobs are removed. `skill_first` and `react` behavior is unchanged. ✓
- **`_is_compound` ownership:** called only inside `_replan_loop`; safe to delete with it. `MAX_SEQ_STEPS` used only in `_replan_loop`; safe to delete. ✓
- **History preserved:** dated plan docs + eval reports keep their replan references (records); only CLAUDE.md (live guidance) is updated, with an explicit "removed" note. ✓
- **Verification is grep-clean + import + full suite** — a dangling reference or shared-helper removal fails loudly. ✓
