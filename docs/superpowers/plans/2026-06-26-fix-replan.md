# Replan Over-Planning Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop `SEQUENCING_MODE=replan` from over-planning and re-running whole skills by (1) resetting the skill-activation budget per sub-step and (2) adding a pure no-progress / skill-repeat de-dup guard that forces an honest finish instead of re-cycling the same sub-goal or skill.

**Architecture:** Two additive changes inside `services/orchestrator/coding_orchestrator.py::AsyncOrchestrator._replan_loop`. First, call `self.skill_router.runner.reset_activations()` at the start of each sub-step (each planner sub-goal is conceptually a fresh mini-task with its own `max_chain` budget). Second, introduce a new pure module `services/orchestrator/replan_guard.py` whose helper inspects the planner's emitted sub-goal against history and returns a stop signal when the planner repeats a near-identical sub-goal or over-uses one skill; `_replan_loop` consults it and finishes early. The `skill_first` and `react` modes never enter `_replan_loop`, so both changes are inert for them.

**Tech Stack:** Python 3.11+, asyncio, pytest, pytest-asyncio (`asyncio_mode = auto`), pytest-bdd, respx (`fake_model` fixture), litellm.

## Global Constraints

These come from `CLAUDE.md` and the existing module; every task implicitly includes them:

- Python files are `snake_case.py`; Python classes are `PascalCase`; Python functions are `snake_case`. (`replan_guard.py`, helper `replan_should_stop`.)
- Never call `print()` / write to stdout in orchestrator code paths; log to stderr via `logging.getLogger(...)` only.
- Every `litellm.acompletion` / `acompletion_with_failover` call must pass `api_key="not-needed"` and `extra_body={"thinking_budget_tokens": <int>}`. (We do NOT add new model calls — both changes are deterministic/pure — so no new call sites to satisfy this on, but do not strip it from existing calls when editing nearby code.)
- Do not use `asyncio.run()` inside an async function.
- Tests live under `tests/` mirroring `services/`; all async tests rely on `asyncio_mode = auto` (no per-test `@pytest.mark.asyncio` needed, but it is harmless). Assert structure, not literal LLM text.
- BDD contract (see `docs/superpowers/plans/2026-06-25-bdd-harness-foundation.md`): feature files live in `tests/services/orchestrator/features/<slug>.feature`, scenarios tagged `@mocked`; step defs in `tests/services/orchestrator/test_<slug>_bdd.py` with `scenarios("features/<slug>.feature")` and `pytestmark = [pytest.mark.bdd, pytest.mark.mocked]`; use `fake_model` (respx HTTP seam) or in-process mocks plus the `run_async` helper from `tests/conftest.py`.
- Additive only: no removals of existing `State` fields, function signatures, or env knobs. New env knob default must preserve current observable behavior except where the explicit goal is to bound it.
- New env knob: `REPLAN_MAX_SKILL_REPEATS` (default `2`) — read at module import in `coding_orchestrator.py`, exactly like the existing `MAX_SEQ_STEPS` / `REPLAN_COMPOUND_GATE` knobs.

---

## File Map

| File | Create/Modify | Responsibility |
|---|---|---|
| `services/orchestrator/replan_guard.py` | Create | Pure, dependency-free helpers: `normalize_subgoal(text)`, `count_skill_uses(history, skill)`, and `replan_should_stop(next_subgoal, history, *, max_skill_repeats)` returning a `ReplanStop` dataclass. No I/O, no model calls, no async. |
| `services/orchestrator/coding_orchestrator.py` | Modify | (a) Add `REPLAN_MAX_SKILL_REPEATS` module env knob next to `MAX_SEQ_STEPS`. (b) In `_replan_loop`, call `reset_activations()` at the top of each sub-step. (c) In `_replan_loop`, after the planner emits `nxt`, consult `replan_should_stop(...)` and break early when it says stop. Record each sub-step's skill name(s) into `history` so the guard can count skill reuse. |
| `tests/services/orchestrator/test_replan_guard.py` | Create | Pure unit tests for `replan_guard.py` (identical / near-identical consecutive sub-goal → stop; distinct → continue; same skill > cap → stop; boundary). |
| `tests/services/orchestrator/test_replan_loop_reset.py` | Create | Unit tests for the `_replan_loop` wiring: `reset_activations()` called once per sub-step; `skill_first` / `react` never call `_replan_loop` (regression safety); guard forces early finish on a repeated-skill replan sequence; no runaway auto-loading within one sub-step. |
| `tests/services/orchestrator/features/replan_progress_guard.feature` | Create | Gherkin behavior for the replan progress guard (`@mocked`). |
| `tests/services/orchestrator/test_replan_progress_guard_bdd.py` | Create | pytest-bdd step defs binding the feature; uses in-process model mock + `run_async`. |

**Interfaces produced by `replan_guard.py` (relied on by later tasks):**

```python
@dataclass(frozen=True)
class ReplanStop:
    stop: bool
    reason: str          # "" when stop is False; else "duplicate_subgoal" | "skill_repeat_cap"

def normalize_subgoal(text: str) -> str: ...
def count_skill_uses(history: list[dict], skill: str) -> int: ...
def replan_should_stop(
    next_subgoal: str,
    history: list[dict],
    *,
    max_skill_repeats: int = 2,
) -> ReplanStop: ...
```

`history` items are the dicts `_replan_loop` already appends, EXTENDED in Task 5 to carry a `skills` key:
`{"step": str, "ok": bool, "summary": str, "skills": list[str]}`. `replan_should_stop` reads `h["step"]` (for duplicate detection) and `h.get("skills", [])` (for skill-repeat counting); it tolerates a missing `skills` key.

---

### Task 1: Pure normalization + skill-count helpers

**Files:**
- Create: `services/orchestrator/replan_guard.py`
- Test: `tests/services/orchestrator/test_replan_guard.py`

**Interfaces:**
- Consumes: nothing (pure stdlib).
- Produces: `normalize_subgoal(text) -> str`, `count_skill_uses(history, skill) -> int` (used by Task 2's `replan_should_stop` and by Task 5's wiring).

- [ ] **Step 1: Write the failing test**

```python
# tests/services/orchestrator/test_replan_guard.py
from __future__ import annotations

from services.orchestrator.replan_guard import normalize_subgoal, count_skill_uses


def test_normalize_lowercases_strips_and_collapses_whitespace():
    assert normalize_subgoal("  Fix  the   Bug ") == "fix the bug"


def test_normalize_strips_trailing_punctuation_and_articles():
    # Near-identical phrasings must normalize to the same string so the
    # duplicate guard treats them as the same sub-goal.
    a = normalize_subgoal("Run repo-fault-localize on the module.")
    b = normalize_subgoal("run repo fault localize on module")
    assert a == b


def test_normalize_empty_and_none_safe():
    assert normalize_subgoal("") == ""
    assert normalize_subgoal(None) == ""  # type: ignore[arg-type]


def test_count_skill_uses_counts_across_history_steps():
    history = [
        {"step": "a", "ok": True, "summary": "", "skills": ["repo-fault-localize"]},
        {"step": "b", "ok": True, "summary": "", "skills": ["code-review", "repo-fault-localize"]},
        {"step": "c", "ok": True, "summary": "", "skills": []},
    ]
    assert count_skill_uses(history, "repo-fault-localize") == 2
    assert count_skill_uses(history, "code-review") == 1
    assert count_skill_uses(history, "nonexistent") == 0


def test_count_skill_uses_tolerates_missing_skills_key():
    history = [{"step": "a", "ok": True, "summary": ""}]  # no "skills"
    assert count_skill_uses(history, "anything") == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/services/orchestrator/test_replan_guard.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.orchestrator.replan_guard'`

- [ ] **Step 3: Write minimal implementation**

```python
# services/orchestrator/replan_guard.py
"""Pure, dependency-free guards for the replan sequencing loop.

These helpers detect when the planner is no longer making progress — it keeps
emitting (near-)identical sub-goals, or it over-uses a single skill across
sub-steps — and tell the loop to finish honestly instead of re-cycling. The
module is intentionally pure (no I/O, no model calls, no async) so it is
trivially unit-testable and free of side effects.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Tokens dropped during normalization so trivially-reworded sub-goals collapse
# to the same canonical string (duplicate detection is a normalized ==).
_ARTICLES = {"the", "a", "an", "on", "of", "to", "for", "in", "into"}
_NON_WORD = re.compile(r"[^a-z0-9\s]+")
_WS = re.compile(r"\s+")


def normalize_subgoal(text: str) -> str:
    """Canonicalize a sub-goal string for equality comparison.

    Lowercase, strip punctuation, drop a small set of articles/prepositions,
    and collapse whitespace. Two sub-goals that differ only in casing,
    punctuation, or filler words normalize to the same value.
    """
    if not text:
        return ""
    lowered = str(text).lower()
    no_punct = _NON_WORD.sub(" ", lowered)
    tokens = [t for t in _WS.sub(" ", no_punct).strip().split(" ") if t and t not in _ARTICLES]
    return " ".join(tokens)


def count_skill_uses(history: list[dict], skill: str) -> int:
    """How many history steps recorded `skill` in their `skills` list."""
    if not skill:
        return 0
    total = 0
    for h in history:
        skills = h.get("skills") or []
        if skill in skills:
            total += 1
    return total
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/services/orchestrator/test_replan_guard.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/replan_guard.py tests/services/orchestrator/test_replan_guard.py
git commit -m "feat(replan): pure normalize_subgoal + count_skill_uses helpers"
```

---

### Task 2: Pure `replan_should_stop` decision helper

**Files:**
- Modify: `services/orchestrator/replan_guard.py`
- Test: `tests/services/orchestrator/test_replan_guard.py`

**Interfaces:**
- Consumes: `normalize_subgoal`, `count_skill_uses` from Task 1.
- Produces: `ReplanStop` dataclass and `replan_should_stop(next_subgoal, history, *, max_skill_repeats=2) -> ReplanStop` (consumed by Task 5's `_replan_loop` wiring and Task 6/7 BDD).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/services/orchestrator/test_replan_guard.py
from services.orchestrator.replan_guard import replan_should_stop, ReplanStop


def _h(step, skills=None):
    return {"step": step, "ok": True, "summary": "", "skills": list(skills or [])}


def test_stop_on_identical_consecutive_subgoal():
    history = [_h("Run repo-fault-localize on the module")]
    res = replan_should_stop("run repo fault localize on module", history)
    assert isinstance(res, ReplanStop)
    assert res.stop is True
    assert res.reason == "duplicate_subgoal"


def test_no_stop_on_distinct_subgoals():
    history = [_h("Generate unit tests for factorial")]
    res = replan_should_stop("Fix the off-by-one bug in factorial", history)
    assert res.stop is False
    assert res.reason == ""


def test_stop_when_same_skill_used_beyond_cap():
    # repo-fault-localize already ran twice; cap is 2 -> a 3rd use must stop.
    history = [
        _h("localize the fault", skills=["repo-fault-localize"]),
        _h("localize it again", skills=["repo-fault-localize"]),
    ]
    res = replan_should_stop("localize the fault once more", history, max_skill_repeats=2)
    assert res.stop is True
    assert res.reason == "skill_repeat_cap"


def test_no_stop_when_skill_under_cap():
    history = [_h("localize the fault", skills=["repo-fault-localize"])]
    res = replan_should_stop("now fix the bug", history, max_skill_repeats=2)
    assert res.stop is False


def test_duplicate_check_only_against_most_recent_step():
    # An older identical step that was followed by a DIFFERENT step is not a
    # no-progress loop; only an immediate repeat of the last step trips.
    history = [_h("review the file"), _h("fix the bug")]
    res = replan_should_stop("review the file", history)
    assert res.stop is False


def test_empty_history_never_stops():
    assert replan_should_stop("do anything", []).stop is False


def test_empty_next_subgoal_does_not_falsely_dup():
    history = [_h("")]
    # an empty next is handled by the loop's own done/empty check, not here;
    # the guard must not crash and must not claim a duplicate on empty==empty.
    res = replan_should_stop("", history)
    assert res.stop is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/services/orchestrator/test_replan_guard.py -q`
Expected: FAIL — `ImportError: cannot import name 'replan_should_stop'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to services/orchestrator/replan_guard.py


@dataclass(frozen=True)
class ReplanStop:
    """Decision returned by `replan_should_stop`.

    stop   -- True when the loop should finish instead of running `next_subgoal`.
    reason -- "" when stop is False; otherwise one of:
              "duplicate_subgoal" | "skill_repeat_cap".
    """
    stop: bool
    reason: str


def _skill_for_next(next_subgoal: str, history: list[dict]) -> str | None:
    """Best-effort: which already-used skill name appears in `next_subgoal`.

    The planner names a skill in its sub-goal text ("Run repo-fault-localize
    on ..."). We only care about skills already present in history, so a 3rd
    invocation of a heavily-reused skill is what trips the cap — we never block
    a brand-new skill the planner has not used yet.
    """
    norm_next = normalize_subgoal(next_subgoal)
    if not norm_next:
        return None
    seen: set[str] = set()
    for h in history:
        for s in (h.get("skills") or []):
            seen.add(s)
    for skill in seen:
        if normalize_subgoal(skill) and normalize_subgoal(skill) in norm_next:
            return skill
    return None


def replan_should_stop(
    next_subgoal: str,
    history: list[dict],
    *,
    max_skill_repeats: int = 2,
) -> ReplanStop:
    """Decide whether the replan loop should stop before running next_subgoal.

    Two no-progress signals:
      1. duplicate_subgoal  -- next_subgoal normalizes equal to the MOST RECENT
         history step (the planner is re-emitting the same step).
      2. skill_repeat_cap   -- next_subgoal targets a skill already used >=
         max_skill_repeats times across history (prevents the "repo-fault-
         localize 4x" thrash).

    Pure: reads only its arguments. Returns ReplanStop(stop=False, reason="")
    when neither signal fires. An empty next_subgoal never trips (the loop's own
    done/empty check owns that case).
    """
    norm_next = normalize_subgoal(next_subgoal)
    if not norm_next:
        return ReplanStop(False, "")

    # (1) immediate duplicate of the last emitted step
    if history:
        if normalize_subgoal(history[-1].get("step", "")) == norm_next:
            return ReplanStop(True, "duplicate_subgoal")

    # (2) skill over-use cap
    skill = _skill_for_next(next_subgoal, history)
    if skill is not None and count_skill_uses(history, skill) >= max_skill_repeats:
        return ReplanStop(True, "skill_repeat_cap")

    return ReplanStop(False, "")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/services/orchestrator/test_replan_guard.py -q`
Expected: PASS (all Task 1 + Task 2 tests green)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/replan_guard.py tests/services/orchestrator/test_replan_guard.py
git commit -m "feat(replan): pure replan_should_stop no-progress / skill-repeat guard"
```

---

### Task 3: Add the `REPLAN_MAX_SKILL_REPEATS` env knob

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py` (module-level knobs block, beside `MAX_SEQ_STEPS` / `REPLAN_COMPOUND_GATE`, structurally after the `SEQUENCING_MODE` definitions)
- Test: `tests/services/orchestrator/test_replan_loop_reset.py`

**Interfaces:**
- Consumes: nothing.
- Produces: module attribute `REPLAN_MAX_SKILL_REPEATS: int` (read by Task 5's `_replan_loop`).

- [ ] **Step 1: Write the failing test**

```python
# tests/services/orchestrator/test_replan_loop_reset.py
from __future__ import annotations

import services.orchestrator.coding_orchestrator as co


def test_replan_max_skill_repeats_default_is_two():
    assert co.REPLAN_MAX_SKILL_REPEATS == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/services/orchestrator/test_replan_loop_reset.py -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'REPLAN_MAX_SKILL_REPEATS'`

- [ ] **Step 3: Write minimal implementation**

Locate the existing knob block (the lines defining `MAX_SEQ_STEPS` and `REPLAN_COMPOUND_GATE`, immediately after the `SEQUENCING_MODE = os.getenv(...)` definition). Add directly below `REPLAN_COMPOUND_GATE`:

```python
# Max times the replan planner may re-target the SAME skill across sub-steps
# before the no-progress guard (replan_guard.replan_should_stop) forces a finish.
# Prevents the live-A/B "repo-fault-localize 4x" thrash. Per-loop, not per-process.
REPLAN_MAX_SKILL_REPEATS = int(os.getenv("REPLAN_MAX_SKILL_REPEATS", "2"))
```

Also add the import at the top of the file, beside the other `from .` imports (e.g. after `from .edit_intent import requires_editing`):

```python
from .replan_guard import replan_should_stop
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/services/orchestrator/test_replan_loop_reset.py -q`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/coding_orchestrator.py tests/services/orchestrator/test_replan_loop_reset.py
git commit -m "feat(replan): REPLAN_MAX_SKILL_REPEATS env knob + replan_guard import"
```

---

### Task 4: Reset the activation budget per sub-step in `_replan_loop`

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py` (`_replan_loop`, inside the `for step in range(MAX_SEQ_STEPS):` body, right before each sub-step is executed)
- Test: `tests/services/orchestrator/test_replan_loop_reset.py`

**Interfaces:**
- Consumes: `self.skill_router.runner.reset_activations()` (existing method on `SkillRunner`).
- Produces: per-sub-step reset behavior relied on by Task 5/7 tests.

Context: today `reset_activations()` is called ONCE per goal in `react_execute` (the block guarded by `if self.skill_router is not None:` near the top of `react_execute`, before mode dispatch). `_replan_loop` runs many sub-steps sharing that one budget, so `SkillRunner.load_skill` hits its `max_chain` cap mid-chain. The fix: reset at the START of each sub-step. The per-goal reset in `react_execute` stays (harmless first reset).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/services/orchestrator/test_replan_loop_reset.py
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.orchestrator.coding_orchestrator import AsyncOrchestrator
from tests.conftest import run_async


def _planner_msg(payload: dict):
    """A litellm-shaped response whose assistant content is JSON `payload`."""
    msg = MagicMock()
    msg.content = json.dumps(payload)
    msg.tool_calls = None
    msg.reasoning_content = ""
    return MagicMock(choices=[MagicMock(message=msg)])


def _make_orch_with_runner():
    runner = MagicMock()
    runner.reset_activations = MagicMock(return_value=None)
    runner.catalog_prompt = MagicMock(return_value="")
    skill_router = MagicMock()
    skill_router.runner = runner
    orch = AsyncOrchestrator(skill_router=skill_router, mcp=AsyncMock(), workspace="/tmp", max_steps=4)
    return orch, runner, skill_router


def test_replan_resets_activations_once_per_substep(monkeypatch):
    """Two real sub-steps -> reset_activations called for each sub-step (>=2),
    on top of the one per-goal reset in react_execute.
    """
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.SEQUENCING_MODE", "replan", raising=False
    )
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.REPLAN_COMPOUND_GATE", False, raising=False
    )
    orch, runner, skill_router = _make_orch_with_runner()

    # skill-first runs each sub-step and returns a distinct result.
    skill_router.run = AsyncMock(return_value={"ok": True, "result": "done", "skill_name": "test-gen"})

    # Planner: step1 -> "generate tests", step2 -> "fix the bug", then done.
    planner_payloads = [
        {"done": False, "next": "Generate and run unit tests", "reason": ""},
        {"done": False, "next": "Fix the off-by-one bug so tests pass", "reason": ""},
        {"done": True, "next": "", "reason": "complete"},
    ]
    # synth call returns plain content; reuse last planner shape via side_effect list.
    side = [_planner_msg(p) for p in planner_payloads] + [_planner_msg({"summary": "ok"})]

    async def _run():
        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock, side_effect=side,
        ):
            return await orch.react_execute("Generate tests AND fix the factorial bug")

    result = run_async(_run())
    assert isinstance(result, dict)
    # one per-goal reset (react_execute) + one per sub-step (2 sub-steps) = >= 3
    assert runner.reset_activations.call_count >= 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/services/orchestrator/test_replan_loop_reset.py::test_replan_resets_activations_once_per_substep -q`
Expected: FAIL — `assert <2> >= 3` (only the single per-goal reset fires today)

- [ ] **Step 3: Write minimal implementation**

In `_replan_loop`, inside `for step in range(MAX_SEQ_STEPS):`, AFTER the planner decision is parsed and the `if done or not nxt: break` check, but BEFORE `skilled = await self._run_skill_first(nxt)`, insert the per-sub-step reset:

```python
                if done or not nxt:
                    break

                # Per-sub-step activation reset. Each planner sub-goal is a fresh
                # mini-task: reset SkillRunner's max_chain budget so load_skill does
                # not hit its activation cap mid-chain across many sub-steps (the
                # documented replan bug). skill_first/react never reach here.
                if self.skill_router is not None:
                    try:
                        self.skill_router.runner.reset_activations()
                    except Exception:
                        pass

                # Execute the sub-step: skill-first, then bounded ReAct fallback so
                # non-skill steps (file edits / fixes) still execute.
                skilled = await self._run_skill_first(nxt)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/services/orchestrator/test_replan_loop_reset.py -q`
Expected: PASS (default + reset test green)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/coding_orchestrator.py tests/services/orchestrator/test_replan_loop_reset.py
git commit -m "fix(replan): reset skill-activation budget per sub-step in _replan_loop"
```

---

### Task 5: Wire `replan_should_stop` into `_replan_loop` (record skills + early finish)

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py` (`_replan_loop`: extend each `history` entry with a `skills` list; consult `replan_should_stop` before executing the sub-step)
- Test: `tests/services/orchestrator/test_replan_loop_reset.py`

**Interfaces:**
- Consumes: `replan_should_stop` (Task 2, imported in Task 3), `REPLAN_MAX_SKILL_REPEATS` (Task 3).
- Produces: early-finish behavior + `history` entries carrying `"skills"` (consumed by the guard and by the BDD steps in Task 7).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/services/orchestrator/test_replan_loop_reset.py

def test_replan_stops_when_planner_repeats_same_skill_beyond_cap(monkeypatch):
    """Planner keeps emitting 'run repo-fault-localize ...'; with cap=2 the loop
    must stop after the 2nd use instead of running it a 3rd/4th time.
    """
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.SEQUENCING_MODE", "replan", raising=False
    )
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.REPLAN_COMPOUND_GATE", False, raising=False
    )
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.REPLAN_MAX_SKILL_REPEATS", 2, raising=False
    )
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.MAX_SEQ_STEPS", 6, raising=False
    )
    orch, runner, skill_router = _make_orch_with_runner()

    run_calls = {"n": 0}

    async def _fake_run(subgoal):
        run_calls["n"] += 1
        return {"ok": True, "result": "located", "skill_name": "repo-fault-localize"}

    skill_router.run = AsyncMock(side_effect=_fake_run)

    # Planner ALWAYS asks to run repo-fault-localize again (never declares done).
    same = {"done": False, "next": "Run repo-fault-localize on the module", "reason": ""}
    side = [_planner_msg(same) for _ in range(6)] + [_planner_msg({"summary": "done"})]

    async def _run():
        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock, side_effect=side,
        ):
            return await orch.react_execute("Find and fix all faults in the module")

    result = run_async(_run())
    # Guard caps skill reuse at 2 -> skill_router.run invoked at most twice,
    # NOT 4x (the live-A/B bug) and NOT MAX_SEQ_STEPS (6) times.
    assert run_calls["n"] <= 2
    assert isinstance(result, dict) and "summary" in result


def test_replan_stops_on_duplicate_subgoal(monkeypatch):
    """Planner emits the SAME sub-goal twice in a row -> loop finishes, does not
    run the duplicate a second time."""
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.SEQUENCING_MODE", "replan", raising=False
    )
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.REPLAN_COMPOUND_GATE", False, raising=False
    )
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.MAX_SEQ_STEPS", 6, raising=False
    )
    orch, runner, skill_router = _make_orch_with_runner()
    run_calls = {"n": 0}

    async def _fake_run(subgoal):
        run_calls["n"] += 1
        return {"ok": True, "result": "x", "skill_name": ""}  # no skill name -> dup path, not cap

    skill_router.run = AsyncMock(side_effect=_fake_run)
    dup = {"done": False, "next": "Review the module for bugs", "reason": ""}
    side = [_planner_msg(dup) for _ in range(6)] + [_planner_msg({"summary": "done"})]

    async def _run():
        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock, side_effect=side,
        ):
            return await orch.react_execute("Review then review the module")

    result = run_async(_run())
    # First sub-goal runs once; the immediate repeat trips duplicate_subgoal -> stop.
    assert run_calls["n"] == 1
    assert isinstance(result, dict)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/services/orchestrator/test_replan_loop_reset.py -q`
Expected: FAIL — both new tests run the skill far more than the cap (the guard is not wired yet; `run_calls["n"]` reaches `MAX_SEQ_STEPS`).

- [ ] **Step 3: Write minimal implementation**

Two edits in `_replan_loop`:

(a) After the per-sub-step reset added in Task 4 and BEFORE executing the sub-step, consult the guard:

```python
                if self.skill_router is not None:
                    try:
                        self.skill_router.runner.reset_activations()
                    except Exception:
                        pass

                # No-progress / skill-repeat guard. If the planner is re-emitting
                # a near-identical sub-goal or over-using one skill, stop instead of
                # re-cycling (prevents the live-A/B 'repo-fault-localize 4x' thrash).
                _stop = replan_should_stop(
                    nxt, history, max_skill_repeats=REPLAN_MAX_SKILL_REPEATS,
                )
                if _stop.stop:
                    await events.emit(
                        "reasoning", node="plan",
                        summary=f"replan stop: {_stop.reason}"[:200],
                        text=_stop.reason,
                    )
                    break

                # Execute the sub-step: skill-first, then bounded ReAct fallback...
                skilled = await self._run_skill_first(nxt)
```

(b) Where the history entry is appended, record the skills used in this sub-step so `count_skill_uses` can see them. Replace the existing `history.append({...})` block with:

```python
                # Skills used this sub-step (skill-first returns the matched skill in
                # tools_used; ReAct fallback returns its tool/skill list there too).
                _step_skills = [
                    t for t in (step_res.get("tools_used") or []) if isinstance(t, str) and t
                ]
                history.append({
                    "step": nxt,
                    "ok": bool(step_res.get("ok")),
                    "summary": str(step_res.get("summary", ""))[:600],
                    "skills": _step_skills,
                })
```

Note: `_run_skill_first` already returns `tools_used=[skill_name]` (line ~424), and `_run_react_loop` returns `tools_used` too — so `_step_skills` is populated for both execution paths.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/services/orchestrator/test_replan_loop_reset.py -q`
Expected: PASS (all reset + guard tests green)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/coding_orchestrator.py tests/services/orchestrator/test_replan_loop_reset.py
git commit -m "fix(replan): stop on duplicate sub-goal / skill-repeat cap via replan_guard"
```

---

### Task 6: Regression safety — `skill_first` / `react` never enter `_replan_loop`; no runaway within a sub-step

**Files:**
- Test: `tests/services/orchestrator/test_replan_loop_reset.py`

**Interfaces:**
- Consumes: `AsyncOrchestrator.react_execute`, `_replan_loop`, `_run_react_loop` (no new production code in this task — it pins the invariants).
- Produces: nothing (regression guard only).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/services/orchestrator/test_replan_loop_reset.py

def test_skill_first_mode_never_calls_replan_loop(monkeypatch):
    """In skill_first mode, _replan_loop must NOT be invoked (the per-sub-step
    reset/guard changes are inert for the default mode)."""
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.SEQUENCING_MODE", "skill_first", raising=False
    )
    orch, runner, skill_router = _make_orch_with_runner()
    skill_router.run = AsyncMock(return_value={"ok": True, "result": "done", "skill_name": "test-gen"})

    called = {"replan": 0}

    async def _spy(self_goal):
        called["replan"] += 1
        return {"ok": True, "summary": "", "tools_used": []}

    async def _run():
        with patch.object(AsyncOrchestrator, "_replan_loop", autospec=True, side_effect=lambda self, g: _spy(g)):
            return await orch.react_execute("review this file for bugs")

    run_async(_run())
    assert called["replan"] == 0


def test_react_mode_never_calls_replan_loop(monkeypatch):
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.SEQUENCING_MODE", "react", raising=False
    )
    orch, runner, skill_router = _make_orch_with_runner()
    called = {"replan": 0}

    async def _spy(self_goal):
        called["replan"] += 1
        return {"ok": True, "summary": "", "tools_used": []}

    # react mode goes straight to _run_react_loop; stub it so no model call is needed.
    async def _fake_loop(goal, max_steps):
        return {"ok": True, "summary": "done", "tools_used": []}

    async def _run():
        with patch.object(AsyncOrchestrator, "_replan_loop", autospec=True, side_effect=lambda self, g: _spy(g)), \
             patch.object(AsyncOrchestrator, "_run_react_loop", autospec=True, side_effect=lambda self, g, m: _fake_loop(g, m)):
            return await orch.react_execute("anything")

    run_async(_run())
    assert called["replan"] == 0


def test_single_substep_reset_does_not_reload_loaded_skill(monkeypatch):
    """The per-sub-step reset clears the activation COUNTER but preserves the
    activation CACHE (runner.loaded). Resetting between sub-steps must not cause
    runaway re-loading WITHIN a single sub-step: load_skill of an already-loaded
    skill returns 'already_loaded' and does not re-read the body.

    Uses a REAL SkillRunner to prove reset_activations() does not clear the cache.
    """
    from pathlib import Path
    from services.skill_runner.skill_runner import SkillRunner

    # Minimal on-disk skill so load_skill has a real body to load.
    import tempfile, os as _os
    d = tempfile.mkdtemp()
    sk = Path(d) / "demo"
    sk.mkdir()
    (sk / "SKILL.md").write_text("---\nname: demo\ndescription: demo skill\n---\nbody\n")
    runner = SkillRunner([Path(d)], max_chain=8)
    runner.discover()

    first = runner.load_skill("demo")
    assert first["response"]["status"] == "loaded"
    runner.reset_activations()  # simulate the per-sub-step reset
    second = runner.load_skill("demo")
    # Cache preserved -> already_loaded, NOT a fresh "loaded" body re-read.
    assert second["response"]["status"] == "already_loaded"
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `python -m pytest tests/services/orchestrator/test_replan_loop_reset.py -q`
Expected: The two mode tests PASS immediately (the dispatch logic in `react_execute` already routes `skill_first`/`react` away from `_replan_loop`); the cache test PASS (reset only zeroes `_activations`, never touches `self.loaded`). If the mode tests FAIL, the dispatch wiring regressed and must be restored. (These are pinning tests; they assert the production invariants the Task 4/5 edits must not break.)

- [ ] **Step 3: No implementation needed**

These are regression-pinning tests over existing behavior. If all pass, proceed. If any fail, the Task 4/5 edits introduced a regression — fix the edit, do not weaken the test.

- [ ] **Step 4: Run the full new-file suite**

Run: `python -m pytest tests/services/orchestrator/test_replan_loop_reset.py tests/services/orchestrator/test_replan_guard.py -q`
Expected: PASS (all green)

- [ ] **Step 5: Commit**

```bash
git add tests/services/orchestrator/test_replan_loop_reset.py
git commit -m "test(replan): pin skill_first/react bypass + cache-preserving reset"
```

---

### Task 7: BDD — replan progress guard feature + step defs

**Files:**
- Create: `tests/services/orchestrator/features/replan_progress_guard.feature`
- Create: `tests/services/orchestrator/test_replan_progress_guard_bdd.py`

**Interfaces:**
- Consumes: `replan_should_stop`, `AsyncOrchestrator.react_execute` in `replan` mode, `run_async` from `tests/conftest.py`.
- Produces: nothing (behavior documentation + executable spec).

- [ ] **Step 1: Write the feature file**

```gherkin
# tests/services/orchestrator/features/replan_progress_guard.feature
Feature: Replan progress guard stops over-planning and skill thrash
  In SEQUENCING_MODE=replan the planner can re-emit a near-identical sub-goal
  or re-target one skill many times (the live A/B saw repo-fault-localize run
  four times). A pure guard detects no-progress and forces an honest finish,
  and the loop resets the skill-activation budget per sub-step so load_skill
  never hits its max_chain cap mid-chain.

  @mocked
  Scenario: An immediate duplicate sub-goal trips the guard
    Given a replan history whose last step is "Review the module for bugs"
    When the planner proposes the next sub-goal "review the module for bugs"
    Then the replan guard says stop
    And the replan stop reason is "duplicate_subgoal"

  @mocked
  Scenario: A distinct next sub-goal does not trip the guard
    Given a replan history whose last step is "Generate unit tests for factorial"
    When the planner proposes the next sub-goal "Fix the off-by-one bug in factorial"
    Then the replan guard says continue

  @mocked
  Scenario: Re-targeting one skill beyond the repeat cap trips the guard
    Given a replan history that has used skill "repo-fault-localize" 2 times
    And the skill repeat cap is 2
    When the planner proposes the next sub-goal "Run repo-fault-localize again on the module"
    Then the replan guard says stop
    And the replan stop reason is "skill_repeat_cap"

  @mocked
  Scenario: The replan loop caps a planner that keeps repeating one skill
    Given a replan orchestrator whose planner always asks to run "repo-fault-localize"
    And the skill repeat cap is 2
    When the compound goal "find and fix every fault" is executed in replan mode
    Then the matching skill is dispatched at most 2 times
    And the activation budget is reset at least once per executed sub-step
```

- [ ] **Step 2: Write the failing step defs**

```python
# tests/services/orchestrator/test_replan_progress_guard_bdd.py
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.replan_guard import replan_should_stop, ReplanStop
from services.orchestrator.coding_orchestrator import AsyncOrchestrator
from tests.conftest import run_async

pytestmark = [pytest.mark.bdd, pytest.mark.mocked]

scenarios("features/replan_progress_guard.feature")


@pytest.fixture
def ctx():
    return {
        "history": [],
        "cap": 2,
        "decision": None,
        "run_calls": 0,
        "reset_calls": 0,
        "result": None,
    }


def _planner_msg(payload: dict):
    msg = MagicMock()
    msg.content = json.dumps(payload)
    msg.tool_calls = None
    msg.reasoning_content = ""
    return MagicMock(choices=[MagicMock(message=msg)])


# ── pure-guard scenarios ───────────────────────────────────────────────────
@given(parsers.parse('a replan history whose last step is "{step}"'))
def _hist_last(ctx, step):
    ctx["history"] = [{"step": step, "ok": True, "summary": "", "skills": []}]


@given(parsers.parse('a replan history that has used skill "{skill}" {n:d} times'))
def _hist_skill(ctx, skill, n):
    ctx["history"] = [
        {"step": f"use {skill} #{i}", "ok": True, "summary": "", "skills": [skill]}
        for i in range(n)
    ]


@given(parsers.parse("the skill repeat cap is {cap:d}"))
def _set_cap(ctx, cap):
    ctx["cap"] = cap


@when(parsers.parse('the planner proposes the next sub-goal "{nxt}"'))
def _propose(ctx, nxt):
    ctx["decision"] = replan_should_stop(nxt, ctx["history"], max_skill_repeats=ctx["cap"])


@then("the replan guard says stop")
def _says_stop(ctx):
    assert isinstance(ctx["decision"], ReplanStop)
    assert ctx["decision"].stop is True


@then("the replan guard says continue")
def _says_continue(ctx):
    assert ctx["decision"].stop is False


@then(parsers.parse('the replan stop reason is "{reason}"'))
def _stop_reason(ctx, reason):
    assert ctx["decision"].reason == reason


# ── wired-loop scenario ────────────────────────────────────────────────────
@given(parsers.parse('a replan orchestrator whose planner always asks to run "{skill}"'))
def _orch_repeat_planner(ctx, skill):
    runner = MagicMock()

    def _reset():
        ctx["reset_calls"] += 1

    runner.reset_activations = MagicMock(side_effect=_reset)
    runner.catalog_prompt = MagicMock(return_value="")
    skill_router = MagicMock()
    skill_router.runner = runner

    async def _run(subgoal):
        ctx["run_calls"] += 1
        return {"ok": True, "result": "located", "skill_name": skill}

    skill_router.run = AsyncMock(side_effect=_run)
    orch = AsyncOrchestrator(skill_router=skill_router, mcp=AsyncMock(), workspace="/tmp", max_steps=4)
    ctx["orch"] = orch
    ctx["skill"] = skill


@when(parsers.parse('the compound goal "{goal}" is executed in replan mode'))
def _exec_replan(ctx, goal, monkeypatch):
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.SEQUENCING_MODE", "replan", raising=False
    )
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.REPLAN_COMPOUND_GATE", False, raising=False
    )
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.REPLAN_MAX_SKILL_REPEATS", ctx["cap"], raising=False
    )
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.MAX_SEQ_STEPS", 6, raising=False
    )
    same = {"done": False, "next": f'Run {ctx["skill"]} on the module', "reason": ""}
    side = [_planner_msg(same) for _ in range(6)] + [_planner_msg({"summary": "done"})]

    async def _run():
        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock, side_effect=side,
        ):
            return await ctx["orch"].react_execute(goal)

    ctx["result"] = run_async(_run())


@then(parsers.parse("the matching skill is dispatched at most {n:d} times"))
def _dispatch_at_most(ctx, n):
    assert ctx["run_calls"] <= n


@then("the activation budget is reset at least once per executed sub-step")
def _reset_per_substep(ctx):
    # at least one reset per executed sub-step (run_calls), plus the per-goal reset.
    assert ctx["reset_calls"] >= ctx["run_calls"]
```

- [ ] **Step 3: Run the BDD suite to verify it passes**

Run: `python -m pytest tests/services/orchestrator/test_replan_progress_guard_bdd.py -q`
Expected: PASS (all scenarios green). If the wired-loop scenario fails on dispatch count, confirm Task 4 + Task 5 edits are present.

- [ ] **Step 4: Run the full replan + guard test set together**

Run: `python -m pytest tests/services/orchestrator/test_replan_guard.py tests/services/orchestrator/test_replan_loop_reset.py tests/services/orchestrator/test_replan_progress_guard_bdd.py -q`
Expected: PASS (all green)

- [ ] **Step 5: Commit**

```bash
git add tests/services/orchestrator/features/replan_progress_guard.feature tests/services/orchestrator/test_replan_progress_guard_bdd.py
git commit -m "test(replan): BDD feature + steps for replan progress guard"
```

---

### Task 8: Whole-suite regression sweep + CLAUDE.md note

**Files:**
- Modify: `CLAUDE.md` (the "Known bug to chase" note in the sequencing section — mark it fixed)

**Interfaces:**
- Consumes: nothing.
- Produces: documentation accuracy.

- [ ] **Step 1: Run the orchestrator suite (regression gate)**

Run: `python -m pytest tests/services/orchestrator/ -q`
Expected: PASS — no previously-green test regresses. (The pre-existing `_pin_skill_first_sequencing` autouse fixture in `test_coding_orchestrator.py` keeps those tests on `skill_first`, so they are unaffected.)

- [ ] **Step 2: Run the full project suite**

Run: `python -m pytest tests/ -q 2>&1 | tail -20`
Expected: PASS — orchestrator + memory suites green (no new failures vs. the branch baseline).

- [ ] **Step 3: Update the CLAUDE.md known-bug note**

In `CLAUDE.md`, find the "Known bug to chase (from the perf branch)" paragraph in the "Sequencing & latency" section. Replace its body so it reflects the fix:

```markdown
**Replan activation-cap bug — FIXED (2026-06-26, branch `feat/agentic-fix-loop`):** `_replan_loop` now calls `self.skill_router.runner.reset_activations()` at the START of each sub-step (each planner sub-goal is a fresh mini-task), so `SkillRunner.load_skill` no longer hits its `max_chain` cap mid-chain. A pure no-progress guard (`services/orchestrator/replan_guard.py::replan_should_stop`) additionally forces an honest finish when the planner re-emits a near-identical sub-goal or re-targets one skill beyond `REPLAN_MAX_SKILL_REPEATS` (default 2), preventing the live-A/B "repo-fault-localize 4x" thrash. `skill_first`/`react` never enter `_replan_loop`, so both are unaffected.
```

- [ ] **Step 4: Verify the doc edit did not break anything**

Run: `git diff --stat`
Expected: shows `CLAUDE.md` plus the new/modified source and test files only.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(CLAUDE.md): mark replan activation-cap bug fixed + de-dup guard"
```

---

## Self-Review

**1. Spec coverage**

- *Change 1 — `reset_activations()` per sub-step* → Task 4 (implementation), Task 6 (regression: `skill_first`/`react` bypass; cache preserved within a sub-step → no runaway auto-load), Task 7 wired scenario (reset ≥ once per executed sub-step). ✔
- *Change 2 — pure de-dup / no-progress helper* → Task 1 + Task 2 (pure helper, fully unit-tested: identical/near-identical consecutive sub-goal → stop; distinct → continue; same skill > cap → stop; boundaries), Task 5 (wiring), Task 7 (BDD). ✔
- *Env knob `REPLAN_MAX_SKILL_REPEATS` default 2* → Task 3. ✔
- *`reset_activations`-per-sub-step regression-safe for `skill_first`/`react`* → Task 6 (`test_skill_first_mode_never_calls_replan_loop`, `test_react_mode_never_calls_replan_loop`). ✔
- *Tested via `fake_model`/scripted replan sequence* → Task 4/5/7 script the planner via `litellm.acompletion` side_effect (the seam `_replan_loop` uses) and `run_async`; this is the in-process equivalent of `fake_model` for the planner path (`_replan_loop` uses `litellm.acompletion` directly, NOT the respx HTTP route, so patching the call seam is the correct mock surface). ✔
- *BDD contract (feature + `@mocked` + `test_<slug>_bdd.py` + unit tests beside)* → Task 7 + Tasks 1–6. ✔
- *Confirm no runaway auto-loading within a single sub-step* → Task 6 `test_single_substep_reset_does_not_reload_loaded_skill` (real `SkillRunner`: reset zeroes the counter but preserves `self.loaded`, so an already-loaded skill returns `already_loaded`). ✔

**2. Placeholder scan** — no "TBD/TODO/handle edge cases/similar to Task N"; every code step shows full code; every run step gives an exact command + expected output. ✔

**3. Type consistency**

- `ReplanStop(stop: bool, reason: str)` — defined Task 2, used identically in Tasks 5/7.
- `replan_should_stop(next_subgoal, history, *, max_skill_repeats=2)` — keyword-only `max_skill_repeats` consistent across Tasks 2/5/7.
- `history` item shape `{"step","ok","summary","skills"}` — `skills` added in Task 5's append; `replan_should_stop` reads `h["step"]` and `h.get("skills", [])` and tolerates the legacy 3-key shape (Task 1 `test_count_skill_uses_tolerates_missing_skills_key`). ✔
- Module knob name `REPLAN_MAX_SKILL_REPEATS` consistent Tasks 3/5/7. ✔
- `normalize_subgoal`, `count_skill_uses` names consistent Tasks 1/2. ✔

**Anchoring note (re-verify before editing):** Task edits anchor on STRUCTURE, not line numbers. In `coding_orchestrator.py`: the module knob block is the `SEQUENCING_MODE`/`MAX_SEQ_STEPS`/`REPLAN_COMPOUND_GATE` group (Task 3); the per-goal reset already lives at the top of `react_execute` under `if self.skill_router is not None:` (leave it); `_replan_loop`'s sub-step body is the `for step in range(MAX_SEQ_STEPS):` block — insert the reset + guard between `if done or not nxt: break` and `skilled = await self._run_skill_first(nxt)` (Tasks 4/5), and extend the `history.append({...})` dict (Task 5). Re-read these structures before editing; do not trust line numbers.
