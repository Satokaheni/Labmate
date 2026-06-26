# Find-and-Fix Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route goals that need file edits / verification ("fix", "make the tests pass", "review then fix the code") into the multi-tool ReAct loop (`_run_react_loop`) instead of the single-skill `skill_first` fast-path, so the model can interleave read + edit + run in one loop.

**Architecture:** Add one pure, deterministic intent classifier `requires_editing(goal: str) -> bool` (new module `services/orchestrator/edit_intent.py`, mirroring the `task_complexity.py` pattern: pure functions, env-driven enable flag, regex/keyword heuristics, frozen-result dataclass). Wire a single additive branch into `AsyncOrchestrator.react_execute`: when the feature flag `ROUTE_EDIT_TO_REACT` is on (default ON) AND `requires_editing(goal)` is True, dispatch directly to `_run_react_loop(goal, self.max_steps)` — bypassing the `skill_first` single-skill short-circuit while keeping skills callable as tools inside the loop. Flag off → byte-identical to today's dispatcher.

**Tech Stack:** Python 3.11, asyncio, pytest + pytest-asyncio, pytest-bdd (respx `fake_model` seam already in `tests/conftest.py`), litellm.

## Global Constraints

- INFERENCE/MODEL: every `litellm.acompletion` / `acompletion_with_failover` call already in the file must keep `api_key="not-needed"` and `extra_body={"thinking_budget_tokens": ...}` (CLAUDE.md rules 6). This feature adds NO new model calls — the classifier is pure regex, zero LLM calls.
- stdout is sacred: never `print()` in orchestrator/MCP code paths; use `logging` to stderr only.
- Python files `snake_case.py`; classes `PascalCase`; functions `snake_case`.
- Tests live under `tests/` mirroring `services/`; `asyncio_mode = auto` (no per-test `@pytest.mark.asyncio` needed but existing tests use it — match the file you edit).
- Assert structure, not literal LLM text. The classifier is deterministic so it MAY be asserted exactly.
- New config and State are **additive only** — no removals, no behavior change when the flag is off.
- Default `ROUTE_EDIT_TO_REACT` = **ON** ("1"), overridable to off via `0/false/no/off` (case-insensitive), mirroring `conditional_gates_enabled()` truthy/falsey parsing but with an ON default.
- The BDD harness already exists: pytest-bdd installed, `fake_model` respx fixture and `run_async` helper in `tests/conftest.py`, `bdd` + `mocked` markers in `pytest.ini`. Do NOT recreate them.

---

## Behavior (BDD) — Gherkin

Create `tests/services/orchestrator/features/find_and_fix_routing.feature` verbatim:

```gherkin
Feature: Route edit/fix/verify-intent goals to the ReAct loop, not single-skill dispatch
  Labmate's default skill_first mode routes a goal to ONE skill. For a
  "review then fix" goal it picks a READ-ONLY skill (code-review / test-gen /
  repo-fault-localize), runs it once, makes zero edits, and stops — a single
  skill dispatch structurally cannot interleave read + edit + run. When a goal
  needs file edits, the orchestrator must instead enter the multi-tool ReAct
  loop (where skills remain callable via call_skill_tool) so the model can
  read, edit, and verify in one loop. Pure read/answer goals keep the existing
  behavior. The whole thing is behind an env flag (default ON) for regression
  safety and A/B.

  Background:
    Given the routing feature flag is on

  # ── Pure classifier behavior ──────────────────────────────────────────────

  @mocked
  Scenario Outline: Edit/fix-intent goals require editing
    Then requires_editing for <goal> is True

    Examples:
      | goal                                                             |
      | "Fix the off-by-one bug in factorial"                            |
      | "Make the tests pass"                                            |
      | "Refactor the parser to use a state machine"                     |
      | "Implement the missing validate() method"                        |
      | "Patch the regex so it accepts unicode"                          |
      | "Review the code and then fix the bugs you find"                 |
      | "Resolve the failing assertion in test_app.py"                   |
      | "Make it work — the import is broken"                            |
      | "Generate tests for sort(), then find and fix the bug"          |
      | "Edit config.py to add a timeout option"                         |

  @mocked
  Scenario Outline: Pure read/answer goals do not require editing
    Then requires_editing for <goal> is False

    Examples:
      | goal                                              |
      | "Summarize what this module does"                 |
      | "What is the capital of France?"                  |
      | "Find bugs in the authentication handler"         |
      | "Explain how the event loop works"                |
      | "Review this file for code smells"                |
      | "List the functions defined in utils.py"          |
      | "What does the parse_header function return?"     |

  @mocked
  Scenario: The A/B compound case c1 (generate then find-and-fix) requires editing
    Then requires_editing for "Generate unit tests for the factorial function, then find and fix the bug" is True

  @mocked
  Scenario: The A/B compound case c2 (review then fix) requires editing
    Then requires_editing for "Review /workspace/ab_buggy.py for bugs, then fix the code" is True

  @mocked
  Scenario: The A/B control case c4 (find bugs, no fix verb) does not require editing
    Then requires_editing for "Find bugs in /workspace/ab_review.py" is False

  @mocked
  Scenario: The flag off disables the classifier (always False)
    Given the routing feature flag is off
    Then requires_editing for "Fix the off-by-one bug in factorial" is False

  # ── Dispatcher wire-in ─────────────────────────────────────────────────────

  @mocked
  Scenario: An edit-intent goal enters the ReAct loop and does not stop after one read-only skill
    Given a skill_first orchestrator whose skill router would match a read-only review skill
    And a fake model that reads, edits, and then finishes
    When the goal "Review the code then fix the bug" is executed
    Then the single-skill fast-path was NOT taken
    And the multi-tool ReAct loop ran
    And react_execute returns ok True

  @mocked
  Scenario: A pure read/answer goal stays on the existing skill_first path
    Given a skill_first orchestrator whose skill router returns a successful read-only result
    When the goal "Summarize what this module does" is executed
    Then the single-skill fast-path WAS taken
    And the summary is the skill result

  @mocked
  Scenario: With the flag off an edit-intent goal keeps today's skill_first behavior
    Given the routing feature flag is off
    And a skill_first orchestrator whose skill router returns a successful read-only result
    When the goal "Fix the bug in factorial" is executed
    Then the single-skill fast-path WAS taken
```

---

## File Map

| File | Create/Modify | Responsibility |
|---|---|---|
| `services/orchestrator/edit_intent.py` | **Create** | Pure deterministic classifier: `EditIntent` frozen dataclass, `route_edit_to_react_enabled()` env reader (default ON), `requires_editing(goal, *, enabled=None) -> bool`. No LLM calls, no I/O. |
| `services/orchestrator/coding_orchestrator.py` | **Modify** (`react_execute`, ~lines 211-247; module-level flag import near lines 14-21) | Import `requires_editing`; add the additive routing branch at the top of `react_execute`'s dispatch section. |
| `tests/services/orchestrator/test_edit_intent.py` | **Create** | Exhaustive unit tests for the pure classifier (edit phrases True, read phrases False, A/B cases, flag off, determinism, case-insensitivity, env parsing). |
| `tests/services/orchestrator/features/find_and_fix_routing.feature` | **Create** | Gherkin above. |
| `tests/services/orchestrator/test_find_and_fix_routing_bdd.py` | **Create** | pytest-bdd step defs binding the feature (classifier scenarios + dispatcher wire-in scenarios). |

---

## Task 1: Pure edit-intent classifier module

**Files:**
- Create: `services/orchestrator/edit_intent.py`
- Test: `tests/services/orchestrator/test_edit_intent.py`

**Interfaces:**
- Produces (later tasks rely on these exact names/signatures):
  - `EditIntent` — frozen dataclass: `requires_edit: bool`, `reason: str`.
  - `route_edit_to_react_enabled() -> bool` — reads env `ROUTE_EDIT_TO_REACT`, **default True**.
  - `requires_editing(goal: str, *, enabled: bool | None = None) -> bool` — pure; returns False when disabled.
  - `classify_edit_intent(goal: str, *, enabled: bool | None = None) -> EditIntent` — the full verdict (`requires_editing` is the bool-only convenience wrapper over it).

- [ ] **Step 1: Write the failing test**

Create `tests/services/orchestrator/test_edit_intent.py`:

```python
"""Edit-intent classifier (deterministic heuristics).

Pure module deciding whether a goal needs file edits / verification — and thus
should enter the multi-tool ReAct loop instead of single-skill dispatch.

Tests verify:
  - EditIntent dataclass structure (frozen)
  - route_edit_to_react_enabled() reads env, defaults ON
  - requires_editing() flags edit/fix/verify phrases True, read/answer phrases False
  - the A/B cases c1/c2 -> True, c4 -> False
  - feature can be disabled (returns False)
  - pure + deterministic + case-insensitive
"""
from __future__ import annotations

import os
import pytest

from services.orchestrator.edit_intent import (
    EditIntent,
    route_edit_to_react_enabled,
    requires_editing,
    classify_edit_intent,
)


class TestEditIntentDataclass:
    def test_has_required_fields(self):
        e = EditIntent(requires_edit=True, reason="fix verb")
        assert e.requires_edit is True
        assert e.reason == "fix verb"

    def test_is_frozen(self):
        e = EditIntent(requires_edit=True, reason="x")
        with pytest.raises(Exception):
            e.requires_edit = False


class TestRouteEditToReactEnabled:
    def test_defaults_to_true(self):
        original = os.environ.pop("ROUTE_EDIT_TO_REACT", None)
        try:
            assert route_edit_to_react_enabled() is True
        finally:
            if original is not None:
                os.environ["ROUTE_EDIT_TO_REACT"] = original

    @pytest.mark.parametrize(
        "falsey", ["0", "false", "False", "FALSE", "no", "No", "off", "Off", "OFF"]
    )
    def test_false_for_falsey(self, falsey):
        original = os.environ.get("ROUTE_EDIT_TO_REACT")
        try:
            os.environ["ROUTE_EDIT_TO_REACT"] = falsey
            assert route_edit_to_react_enabled() is False
        finally:
            if original is not None:
                os.environ["ROUTE_EDIT_TO_REACT"] = original
            else:
                os.environ.pop("ROUTE_EDIT_TO_REACT", None)

    @pytest.mark.parametrize("truthy", ["1", "true", "True", "yes", "on", "ON"])
    def test_true_for_truthy(self, truthy):
        original = os.environ.get("ROUTE_EDIT_TO_REACT")
        try:
            os.environ["ROUTE_EDIT_TO_REACT"] = truthy
            assert route_edit_to_react_enabled() is True
        finally:
            if original is not None:
                os.environ["ROUTE_EDIT_TO_REACT"] = original
            else:
                os.environ.pop("ROUTE_EDIT_TO_REACT", None)


# Phrases that MUST be flagged as needing edits.
EDIT_GOALS = [
    "Fix the off-by-one bug in factorial",
    "Make the tests pass",
    "Refactor the parser to use a state machine",
    "Implement the missing validate() method",
    "Patch the regex so it accepts unicode",
    "Review the code and then fix the bugs you find",
    "Resolve the failing assertion in test_app.py",
    "Make it work — the import is broken",
    "Generate tests for sort(), then find and fix the bug",
    "Edit config.py to add a timeout option",
    "Correct the typo that breaks the build",
    "Apply the fix and rerun the suite",
    "Debug and repair the broken endpoint",
]

# Phrases that MUST NOT be flagged (pure read / answer / analysis).
READ_GOALS = [
    "Summarize what this module does",
    "What is the capital of France?",
    "Find bugs in the authentication handler",
    "Explain how the event loop works",
    "Review this file for code smells",
    "List the functions defined in utils.py",
    "What does the parse_header function return?",
    "Describe the architecture of the orchestrator",
    "Search for papers about retrieval augmentation",
]


class TestRequiresEditing:
    @pytest.mark.parametrize("goal", EDIT_GOALS)
    def test_edit_goals_true(self, goal):
        assert requires_editing(goal, enabled=True) is True, goal

    @pytest.mark.parametrize("goal", READ_GOALS)
    def test_read_goals_false(self, goal):
        assert requires_editing(goal, enabled=True) is False, goal

    def test_ab_case_c1_true(self):
        assert requires_editing(
            "Generate unit tests for the factorial function, then find and fix the bug",
            enabled=True,
        ) is True

    def test_ab_case_c2_true(self):
        assert requires_editing(
            "Review /workspace/ab_buggy.py for bugs, then fix the code",
            enabled=True,
        ) is True

    def test_ab_case_c4_false(self):
        assert requires_editing(
            "Find bugs in /workspace/ab_review.py", enabled=True
        ) is False

    def test_disabled_returns_false(self):
        # Even an obvious edit goal is False when the feature is off.
        assert requires_editing("Fix the bug", enabled=False) is False

    def test_none_enabled_consults_env_default_on(self):
        original = os.environ.pop("ROUTE_EDIT_TO_REACT", None)
        try:
            assert requires_editing("Fix the bug", enabled=None) is True
        finally:
            if original is not None:
                os.environ["ROUTE_EDIT_TO_REACT"] = original

    def test_deterministic(self):
        g = "Refactor and fix the parser"
        assert requires_editing(g, enabled=True) == requires_editing(g, enabled=True)

    def test_case_insensitive(self):
        assert requires_editing("FIX THE BUG", enabled=True) is True
        assert requires_editing("fix the bug", enabled=True) is True

    def test_find_bugs_then_fix_is_true(self):
        # "find bugs" alone is read-only, but a trailing fix verb flips it.
        assert requires_editing("Find bugs in app.py and fix them", enabled=True) is True

    def test_review_without_fix_is_false(self):
        assert requires_editing("Review app.py", enabled=True) is False


class TestClassifyEditIntent:
    def test_returns_editintent_with_reason(self):
        v = classify_edit_intent("Fix the bug", enabled=True)
        assert isinstance(v, EditIntent)
        assert v.requires_edit is True
        assert v.reason  # non-empty

    def test_disabled_reason(self):
        v = classify_edit_intent("Fix the bug", enabled=False)
        assert v.requires_edit is False
        assert "disabled" in v.reason.lower()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_edit_intent.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.orchestrator.edit_intent'`.

- [ ] **Step 3: Write the implementation**

Create `services/orchestrator/edit_intent.py`:

```python
"""Edit-intent classifier — deterministic heuristics for find-and-fix routing.

Decides whether a goal needs FILE EDITS / VERIFICATION (and so should enter the
multi-tool ReAct loop, where the model interleaves read + edit + run) versus a
pure read/answer goal (which keeps the existing single-skill dispatch).

The classifier is pure and deterministic: the same goal always yields the same
verdict, with no LLM call and no I/O. It is intentionally CONSERVATIVE — it only
returns True when an explicit edit/fix/verify verb is present, so a pure
"summarize" / "find bugs" / "review" goal is never mis-routed.

Rule (documented so it can be audited):
  1. If an UNCONDITIONAL edit verb appears as a word (fix, edit, patch, refactor,
     implement, rewrite, modify, repair, correct, resolve, debug, apply ...),
     -> requires edit.
  2. If a VERIFICATION phrase appears ("make the tests pass", "make it work",
     "get the tests green", "so the tests pass") -> requires edit.
  3. Otherwise -> no edit (pure read/answer). "review", "find bugs", "summarize",
     "explain", "what/why/how" questions are read-only UNLESS an edit verb from
     rule 1 also appears later in the goal (e.g. "review then fix").
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class EditIntent:
    """Immutable edit-intent verdict for a goal."""

    requires_edit: bool
    reason: str


_FALSEY = {"", "0", "false", "no", "off"}


def route_edit_to_react_enabled() -> bool:
    """Whether edit-intent goals route to the ReAct loop.

    Reads env ``ROUTE_EDIT_TO_REACT``. Default ON (returns True when unset).
    Falsey values ("0", "false", "no", "off", case-insensitive) -> False.
    """
    return os.getenv("ROUTE_EDIT_TO_REACT", "1").strip().lower() not in _FALSEY


# Rule 1: explicit edit/fix verbs. Word-boundary matched so "prefix" / "fixture"
# do NOT match "fix", and "implementation" matches "implement" only as a stem
# via the explicit alternatives below. These are verbs that imply mutating files.
_EDIT_VERBS = (
    r"fix(?:es|ed|ing)?",
    r"edit(?:s|ed|ing)?",
    r"patch(?:es|ed|ing)?",
    r"refactor(?:s|ed|ing)?",
    r"implement(?:s|ed|ing)?",
    r"rewrite|rewrites|rewriting|rewrote",
    r"modif(?:y|ies|ied|ying)",
    r"repair(?:s|ed|ing)?",
    r"correct(?:s|ed|ing)?",
    r"resolv(?:e|es|ed|ing)",
    r"debug(?:s|ged|ging)?",
    r"appl(?:y|ies|ied|ying)",
    r"updat(?:e|es|ed|ing)",
)
_EDIT_VERB_RE = re.compile(
    r"\b(?:" + "|".join(_EDIT_VERBS) + r")\b", re.IGNORECASE
)

# Rule 2: verification phrases — "make the tests pass", "make it work", etc.
# These imply the agent must change code until a check passes.
_VERIFY_PHRASE_RE = re.compile(
    r"(?:"
    r"make\s+(?:the\s+|all\s+)?tests?\s+pass"
    r"|get\s+(?:the\s+)?tests?\s+(?:to\s+)?(?:pass|green|passing)"
    r"|so\s+(?:the\s+|all\s+)?tests?\s+pass"
    r"|make\s+it\s+work"
    r"|get\s+it\s+working"
    r"|make\s+the\s+build\s+(?:pass|green|work)"
    r")",
    re.IGNORECASE,
)


def classify_edit_intent(
    goal: str,
    *,
    enabled: bool | None = None,
) -> EditIntent:
    """Classify whether ``goal`` needs file edits / verification.

    Args:
        goal: The goal/sub-goal text.
        enabled: Explicit feature toggle. If None, consults
            route_edit_to_react_enabled(). If False, always returns no-edit.

    Returns:
        EditIntent(requires_edit, reason).
    """
    if enabled is None:
        enabled = route_edit_to_react_enabled()

    if not enabled:
        return EditIntent(requires_edit=False, reason="feature disabled")

    text = goal or ""

    if _EDIT_VERB_RE.search(text):
        return EditIntent(requires_edit=True, reason="explicit edit/fix verb")

    if _VERIFY_PHRASE_RE.search(text):
        return EditIntent(requires_edit=True, reason="verification phrase (make tests pass / make it work)")

    return EditIntent(
        requires_edit=False,
        reason="read/answer goal (no edit verb or verification phrase)",
    )


def requires_editing(goal: str, *, enabled: bool | None = None) -> bool:
    """Bool-only convenience wrapper over classify_edit_intent()."""
    return classify_edit_intent(goal, enabled=enabled).requires_edit
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_edit_intent.py -q`
Expected: PASS — all parametrized cases green.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/edit_intent.py tests/services/orchestrator/test_edit_intent.py
git commit -m "feat(orchestrator): pure edit-intent classifier for find-and-fix routing

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Wire the classifier into `react_execute`

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py` (import near lines 14-21; dispatch branch in `react_execute`, lines 234-247)
- Test: `tests/services/orchestrator/test_coding_orchestrator.py` (add tests to the existing file — see Step 1)

**Interfaces:**
- Consumes from Task 1: `requires_editing(goal, *, enabled=None) -> bool`.
- Produces: `react_execute` gains an additive branch. No signature change. When `requires_editing(goal)` is True it calls `self._run_react_loop(goal, self.max_steps)` and returns, bypassing `_run_skill_first`. Behavior is unchanged for `SEQUENCING_MODE == "react"` (already runs the loop) and for `replan` (handled before this branch — see Step 3 placement note).

- [ ] **Step 1: Write the failing tests**

Append to `tests/services/orchestrator/test_coding_orchestrator.py` (new class at end of file). The autouse fixture `_pin_skill_first_sequencing` already pins `SEQUENCING_MODE="skill_first"` for this file, so these tests exercise exactly the path we're changing:

```python
class TestEditIntentRouting:
    """react_execute routes edit-intent goals to the ReAct loop, not single-skill."""

    def _make_orch(self):
        from services.orchestrator.coding_orchestrator import AsyncOrchestrator
        # A skill_router whose .run() would succeed with a read-only result, so if
        # the fast-path were taken we'd see THAT summary (and never reach the loop).
        router = MagicMock()
        router.runner = MagicMock()
        router.runner.reset_activations = MagicMock()
        router.run = AsyncMock(return_value={"ok": True, "result": "read-only review output"})
        return AsyncOrchestrator(skill_router=router, mcp=AsyncMock(), workspace="/tmp", max_steps=4)

    @pytest.mark.asyncio
    async def test_edit_goal_enters_react_loop_not_skill_first(self):
        """An edit-intent goal must bypass _run_skill_first and run the ReAct loop."""
        orch = self._make_orch()

        skill_first_calls = {"n": 0}

        async def _spy_skill_first(goal):
            skill_first_calls["n"] += 1
            return {"ok": True, "summary": "SHOULD NOT BE CALLED"}

        # Fake model: finish immediately so the loop returns ok=True quickly.
        finish_msg = MagicMock()
        finish_msg.content = None
        tc = MagicMock()
        tc.id = "c1"
        tc.function.name = "finish"
        tc.function.arguments = json.dumps({"summary": "fixed the bug"})
        finish_msg.tool_calls = [tc]
        resp = MagicMock(choices=[MagicMock(message=finish_msg)])

        with patch.object(orch, "_run_skill_first", side_effect=_spy_skill_first), \
             patch.object(orch, "_run_react_loop", wraps=orch._run_react_loop) as loop_spy, \
             patch("services.orchestrator.coding_orchestrator.acompletion_with_failover",
                   new_callable=AsyncMock, return_value=resp):
            result = await orch.react_execute("Review the code then fix the bug", )

        assert skill_first_calls["n"] == 0, "skill-first fast-path must be skipped for edit goals"
        assert loop_spy.called, "the multi-tool ReAct loop must run for edit goals"
        assert result["ok"] is True
        assert "fixed the bug" in result["summary"]

    @pytest.mark.asyncio
    async def test_read_goal_stays_on_skill_first(self):
        """A pure read/answer goal keeps the existing single-skill fast-path."""
        orch = self._make_orch()
        with patch.object(orch, "_run_react_loop",
                          new_callable=AsyncMock) as loop_spy:
            result = await orch.react_execute("Summarize what this module does")
        assert not loop_spy.called, "read goals must NOT enter the ReAct loop via the edit branch"
        assert result["ok"] is True
        assert "read-only review output" in result["summary"]

    @pytest.mark.asyncio
    async def test_flag_off_keeps_skill_first_for_edit_goal(self, monkeypatch):
        """ROUTE_EDIT_TO_REACT=0 -> edit goals keep today's skill_first behavior."""
        monkeypatch.setenv("ROUTE_EDIT_TO_REACT", "0")
        orch = self._make_orch()
        with patch.object(orch, "_run_react_loop",
                          new_callable=AsyncMock) as loop_spy:
            result = await orch.react_execute("Fix the bug in factorial")
        assert not loop_spy.called, "flag off must preserve today's skill_first path"
        assert "read-only review output" in result["summary"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest "tests/services/orchestrator/test_coding_orchestrator.py::TestEditIntentRouting" -q`
Expected: FAIL — `test_edit_goal_enters_react_loop_not_skill_first` fails because today `_run_skill_first` IS called for an edit goal (skill_first short-circuit), so `skill_first_calls["n"] == 1`.

- [ ] **Step 3: Write the implementation**

In `services/orchestrator/coding_orchestrator.py`, add the import alongside the other orchestrator imports (after line 20, `from .iteration_budget import IterationBudget, CHEAP_TOOLS`):

```python
from .edit_intent import requires_editing
```

Then in `react_execute`, insert the additive routing branch. The current dispatch block (lines 234-247) is:

```python
        if SEQUENCING_MODE == "replan":
            return await self._replan_loop(goal)
        if SEQUENCING_MODE != "react":
            skilled = await self._run_skill_first(goal)
            if skilled is not None:
                return skilled
        return await self._run_react_loop(goal, self.max_steps)
```

Replace it with (replan is unchanged and handled first; the new branch sits between replan and the skill_first short-circuit, so it only affects skill_first mode — `react` already runs the loop):

```python
        if SEQUENCING_MODE == "replan":
            return await self._replan_loop(goal)

        # Find-and-fix routing: a goal that needs file edits / verification
        # ("fix", "make the tests pass", "review then fix the code") cannot be
        # served by a single read-only skill dispatch — it must enter the
        # multi-tool ReAct loop so the model can interleave read + edit + run
        # (skills stay callable inside the loop via call_skill_tool). Gated by
        # ROUTE_EDIT_TO_REACT (default ON); when off, behavior is identical to
        # before. No effect in 'react' mode (already runs the loop).
        if SEQUENCING_MODE != "react" and requires_editing(goal):
            return await self._run_react_loop(goal, self.max_steps)

        if SEQUENCING_MODE != "react":
            skilled = await self._run_skill_first(goal)
            if skilled is not None:
                return skilled
        return await self._run_react_loop(goal, self.max_steps)
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest "tests/services/orchestrator/test_coding_orchestrator.py::TestEditIntentRouting" -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Run the whole orchestrator suite for regression safety**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/ -q`
Expected: PASS — all existing tests still green (the autouse fixture pins skill_first; existing skill-first tests use non-edit goals like "what is 2+2?", "use test-skill", "echo hello", "simple task", "do something", which `requires_editing` returns False for, so they keep the fast-path).

Note for the implementer: scan the existing `react_execute` skill-first tests for any goal string that `requires_editing` would now flag True. The audited set ("do something", "use test-skill", "echo hello", "simple task", "what is 2+2?", "some goal that matches a skill", "run echo") all contain NO edit verb -> all stay False -> no regression. If any future test uses an edit verb and expects the fast-path, it must set `ROUTE_EDIT_TO_REACT=0` or assert the loop path.

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/coding_orchestrator.py tests/services/orchestrator/test_coding_orchestrator.py
git commit -m "feat(orchestrator): route edit/fix-intent goals to the ReAct loop

react_execute now sends goals that need file edits/verification into
_run_react_loop instead of single-skill dispatch, so the model can
interleave read+edit+run. Gated by ROUTE_EDIT_TO_REACT (default ON);
flag off is byte-identical to prior behavior.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: BDD feature + step definitions

**Files:**
- Create: `tests/services/orchestrator/features/find_and_fix_routing.feature` (Gherkin from the "Behavior (BDD)" section above — copy verbatim)
- Create: `tests/services/orchestrator/test_find_and_fix_routing_bdd.py`

**Interfaces:**
- Consumes: `requires_editing` (Task 1), `react_execute` / `_run_react_loop` / `_run_skill_first` (Task 2), and `run_async` from `tests/conftest.py` (already exists, used by the other `*_bdd.py` files).

- [ ] **Step 1: Create the feature file**

Create `tests/services/orchestrator/features/find_and_fix_routing.feature` with the EXACT Gherkin from the "Behavior (BDD) — Gherkin" section above. Do not paraphrase — the step parsers below match it literally.

- [ ] **Step 2: Write the step definitions (the failing test)**

Create `tests/services/orchestrator/test_find_and_fix_routing_bdd.py`:

```python
# tests/services/orchestrator/test_find_and_fix_routing_bdd.py
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.edit_intent import requires_editing
from tests.conftest import run_async

pytestmark = [pytest.mark.bdd, pytest.mark.mocked]

scenarios("features/find_and_fix_routing.feature")


@pytest.fixture
def ctx():
    return {"enabled": True, "orch": None, "result": None,
            "skill_first_calls": 0, "loop_ran": False}


# ── Background / flag ──────────────────────────────────────────────────────
@given("the routing feature flag is on")
def _flag_on(ctx):
    ctx["enabled"] = True


@given("the routing feature flag is off")
def _flag_off(ctx):
    ctx["enabled"] = False


# ── Pure classifier scenarios ──────────────────────────────────────────────
@then(parsers.parse("requires_editing for {goal} is {expected}"))
def _requires_editing_is(ctx, goal, expected):
    # Gherkin passes the goal token possibly wrapped in quotes (from Examples
    # tables and inline strings). Strip surrounding quotes if present.
    g = goal.strip()
    if len(g) >= 2 and g[0] == g[-1] and g[0] in {'"', "'"}:
        g = g[1:-1]
    want = expected.strip() == "True"
    assert requires_editing(g, enabled=ctx["enabled"]) is want, (g, expected)


# ── Dispatcher wire-in scenarios ───────────────────────────────────────────
def _build_orch(ctx):
    from services.orchestrator.coding_orchestrator import AsyncOrchestrator
    router = MagicMock()
    router.runner = MagicMock()
    router.runner.reset_activations = MagicMock()
    router.run = AsyncMock(return_value={"ok": True, "result": "read-only review output"})
    orch = AsyncOrchestrator(skill_router=router, mcp=AsyncMock(), workspace="/tmp", max_steps=4)
    ctx["orch"] = orch
    return orch


@given("a skill_first orchestrator whose skill router would match a read-only review skill")
def _orch_readonly_match(ctx, monkeypatch):
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.SEQUENCING_MODE",
        "skill_first", raising=False,
    )
    _build_orch(ctx)


@given("a skill_first orchestrator whose skill router returns a successful read-only result")
def _orch_readonly_result(ctx, monkeypatch):
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.SEQUENCING_MODE",
        "skill_first", raising=False,
    )
    _build_orch(ctx)


@given("a fake model that reads, edits, and then finishes")
def _fake_model_edits(ctx):
    # The ReAct loop, if entered, gets a single 'finish' turn so it returns fast.
    finish_msg = MagicMock()
    finish_msg.content = None
    tc = MagicMock()
    tc.id = "c1"
    tc.function.name = "finish"
    tc.function.arguments = json.dumps({"summary": "read, edited, fixed"})
    finish_msg.tool_calls = [tc]
    ctx["_fake_resp"] = MagicMock(choices=[MagicMock(message=finish_msg)])


@when(parsers.parse('the goal "{goal}" is executed'))
def _execute(ctx, goal):
    orch = ctx["orch"]

    # Apply the flag-off case by env if the Background set it off.
    import os
    env_patch = {}
    if ctx["enabled"] is False:
        env_patch["ROUTE_EDIT_TO_REACT"] = "0"

    # Spy on the two paths.
    real_skill_first = orch._run_skill_first
    real_loop = orch._run_react_loop

    async def _spy_skill_first(g):
        ctx["skill_first_calls"] += 1
        return await real_skill_first(g)

    async def _spy_loop(g, n):
        ctx["loop_ran"] = True
        return await real_loop(g, n)

    resp = ctx.get("_fake_resp") or MagicMock(
        choices=[MagicMock(message=MagicMock(content="ok", tool_calls=None))]
    )

    async def _run():
        with patch.dict(os.environ, env_patch), \
             patch.object(orch, "_run_skill_first", side_effect=_spy_skill_first), \
             patch.object(orch, "_run_react_loop", side_effect=_spy_loop), \
             patch("services.orchestrator.coding_orchestrator.acompletion_with_failover",
                   new_callable=AsyncMock, return_value=resp):
            return await orch.react_execute(goal)

    ctx["result"] = run_async(_run())


@then("the single-skill fast-path was NOT taken")
def _fast_path_not_taken(ctx):
    assert ctx["skill_first_calls"] == 0


@then("the single-skill fast-path WAS taken")
def _fast_path_taken(ctx):
    assert ctx["skill_first_calls"] >= 1


@then("the multi-tool ReAct loop ran")
def _loop_ran(ctx):
    assert ctx["loop_ran"] is True


@then("react_execute returns ok True")
def _ok_true(ctx):
    assert ctx["result"]["ok"] is True


@then("the summary is the skill result")
def _summary_is_skill(ctx):
    assert "read-only review output" in ctx["result"]["summary"]
```

- [ ] **Step 3: Run the BDD tests to verify they pass**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_find_and_fix_routing_bdd.py -q`
Expected: PASS — every scenario (classifier Examples tables + 3 dispatcher scenarios) green.

- [ ] **Step 4: Run the full orchestrator + memory suite (no regressions)**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/ tests/services/memory/ -q`
Expected: PASS — prior suite count (684) + the new edit_intent unit tests + new BDD scenarios, all green.

- [ ] **Step 5: Commit**

```bash
git add tests/services/orchestrator/features/find_and_fix_routing.feature tests/services/orchestrator/test_find_and_fix_routing_bdd.py
git commit -m "test(orchestrator): BDD coverage for find-and-fix routing

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage**
- Pure deterministic classifier `requires_editing(goal) -> bool` in a new module → Task 1. ✓
- Conservative keyword + phrase heuristics; edit verbs (fix/edit/patch/refactor/implement/rewrite/modify/repair/correct/resolve/debug/apply/update) + verification phrases ("make the tests pass", "make it work") → Task 1 `_EDIT_VERBS`, `_VERIFY_PHRASE_RE`. ✓
- Does NOT flag pure read/answer ("summarize", "what is", "find bugs in" without a fix verb, "explain", "review") → Task 1 READ_GOALS tests + the rule doc. ✓
- Documented rule → module docstring + inline comments. ✓
- Dispatcher routes edit-intent goals to `_run_react_loop(goal, self.max_steps)` regardless of skill_first default, skills still callable inside → Task 2 branch (calls `_run_react_loop`; loop already exposes `call_skill_tool`). ✓
- Otherwise keeps current behavior per SEQUENCING_MODE → Task 2 leaves replan/react/skill_first fall-through intact. ✓
- Gated behind env flag `ROUTE_EDIT_TO_REACT`, default ON, overridable → Task 1 `route_edit_to_react_enabled()`. ✓
- Regression-safe (flag off → identical) → Task 2 Step 3 branch is guarded by `requires_editing(goal)` which is False when disabled; Task 2 `test_flag_off_keeps_skill_first_for_edit_goal`. ✓
- Exhaustive unit tests incl. A/B c1/c2 True, c4 False → Task 1 `test_ab_case_c1/c2/c4`. ✓
- BDD: review-then-fix enters loop / doesn't stop after one read-only skill; summarize/find-bugs stays on existing path; flag-off preserves behavior → Task 3 feature scenarios. ✓
- Uses existing BDD harness (fake_model/respx, run_async, bdd marker), does not recreate → Task 3 imports from `tests/conftest.py`, reuses markers. ✓
- New State/config additive only → only a new env var + a new module; no `State` field added (the routing decision is computed at dispatch time, not persisted — keeps it strictly additive and avoids checkpoint-schema churn). ✓

**2. Placeholder scan** — no TBD/TODO/"add error handling"/"similar to". Every code step shows full code; every run step shows command + expected output. ✓

**3. Type consistency** — `requires_editing(goal, *, enabled=None) -> bool` and `classify_edit_intent(...) -> EditIntent` are referenced identically in Tasks 1, 2, 3. `EditIntent(requires_edit, reason)` field names consistent. `route_edit_to_react_enabled()` consistent. Dispatcher calls `self._run_react_loop(goal, self.max_steps)` matching the real signature `_run_react_loop(self, goal, max_steps)`. Tests patch `acompletion_with_failover` (what `_run_react_loop` actually calls) for the loop-entering scenario, matching the existing test at line 326. ✓

Note on the one design decision the spec left open: **no new `State` field** is added. The edit-intent decision is local to `react_execute` and recomputed deterministically from the goal, so persisting it would be redundant and would touch the checkpoint schema. Config is the single new env var `ROUTE_EDIT_TO_REACT`. This satisfies "new additive State/config only" by adding additive config and zero State changes.
```

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-06-26-find-and-fix-routing.md`.**
