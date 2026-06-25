# Conditional Gates (skip ambiguity + verify for trivial tasks) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a cheap, deterministic task-complexity classifier so the graph can skip the `assess_ambiguity` gate and/or the `verify` gate for clearly trivial/low-risk tasks, while keeping both gates mandatory for non-trivial work.

**Architecture:** A pure, LLM-free helper (`services/orchestrator/task_complexity.py`) returns a small frozen dataclass `Complexity(skip_ambiguity, skip_verify, reason)`. The `assess_ambiguity` node computes it once and stashes the result on `State` (additive fields). `ambiguity_router` short-circuits to `plan` when `skip_ambiguity` is set; `verify_router` short-circuits to `check` when `skip_verify` is set. A master env flag (`ENABLE_CONDITIONAL_GATES`, **OFF by default**) makes the whole feature a no-op when disabled — behavior is then byte-for-byte identical to today.

**Tech Stack:** Python 3.11+, LangGraph `StateGraph`, pytest, pytest-asyncio, pytest-bdd, respx (already wired by the foundation plan via the `fake_model` fixture in `tests/conftest.py`).

## Global Constraints

- Do NOT import `tiktoken` anywhere. If token-counting is ever needed use the Gemma tokenizer (not needed in this plan — the classifier is regex/length-based only).
- Do NOT modify `core/`, `tools/`, or `main.py` (M2 baseline).
- All new behavior must be additive to `State` (TypedDict in `services/orchestrator/types.py`) — never remove or repurpose existing fields.
- Classifier MUST be pure + deterministic + LLM-free (no network, no model calls, no clock, no randomness).
- The feature is **OFF by default**: `ENABLE_CONDITIONAL_GATES` defaults to disabled, and skips are conservative (only fire when the task is *clearly* trivial). With the flag off, `ambiguity_router` / `verify_router` behave exactly as they do today.
- Python files: `snake_case.py`. Python classes: PascalCase. Functions/methods: `snake_case`.
- New env knobs read via `os.getenv` with defaults in code (match the existing `ENABLE_DIRECT_ANSWER_FASTPATH` truthiness convention: a value in `("0", "false", "False", "")` means OFF).
- New mocked tests carry `@pytest.mark.mocked`; async tests carry `@pytest.mark.asyncio`. BDD scenarios are tagged `@mocked`.
- Reuse the existing direct-answer trivial-task notion where possible: a task that the direct-answer fast-path would answer (skill-less single intent) is the canonical "trivial" signal. The classifier's deterministic heuristic is a conservative *superset* gate layered on top — it must never classify something trivial that the ambiguity model would flag as ambiguous.

---

## Behavior (BDD) — Gherkin

This is the full content of `tests/services/orchestrator/features/conditional_gates.feature`. It is created verbatim in Task 4.

```gherkin
@mocked
Feature: Conditional gates skip ambiguity and verify for trivial tasks
  The orchestrator runs an LLM ambiguity gate on every task and a critique
  gate on every code/writing artifact. For clearly trivial, low-risk tasks
  this is wasted latency. A deterministic complexity classifier lets the graph
  skip those gates for trivial work while keeping them for everything else.
  The whole feature is OFF by default and must be a strict no-op when disabled.

  Background:
    Given the conditional-gates feature is enabled

  Scenario: A trivial arithmetic task skips both gates
    Given a task "What is 2+2?"
    When the orchestrator classifies the task complexity
    Then the classifier marks skip_ambiguity true
    And the classifier marks skip_verify true
    And the ambiguity gate is skipped so routing goes straight to plan
    And the verify gate is skipped so routing goes straight to check

  Scenario: An ambiguous task is still gated
    Given a task "make it better"
    When the orchestrator classifies the task complexity
    Then the classifier marks skip_ambiguity false
    And the ambiguity gate still runs and produces an ambiguity score

  Scenario: A code artifact for a non-trivial task is still verified
    Given a task "Implement a rate limiter with a sliding window and tests"
    When the orchestrator classifies the task complexity
    Then the classifier marks skip_verify false
    And the verify gate still runs and routes a low-scoring artifact to reflect

  Scenario: With the feature flag off, behavior is unchanged
    Given the conditional-gates feature is disabled
    And a task "What is 2+2?"
    When the orchestrator classifies the task complexity
    Then the classifier marks skip_ambiguity false
    And the classifier marks skip_verify false
    And the ambiguity gate still runs and produces an ambiguity score
```

---

## File Map

| File | Responsibility | Action |
|------|----------------|--------|
| `services/orchestrator/task_complexity.py` | Pure, deterministic, LLM-free complexity classifier; defines `Complexity` dataclass, `classify_complexity()`, the env-flag reader, and the regex/length heuristics. | Create |
| `services/orchestrator/types.py` | Add additive `State` fields: `complexity`, `skip_ambiguity`, `skip_verify`. | Modify (`State`, after line 69) |
| `services/orchestrator/graph.py` | Import the classifier; compute it in `assess_ambiguity`, stash skip flags on the returned delta, and short-circuit when `skip_ambiguity`; short-circuit `ambiguity_router` and `verify_router` on the committed skip flags. | Modify (imports near line 13; `assess_ambiguity` 438-541; `ambiguity_router` 644-649; `verify_router` 652-665) |
| `tests/services/orchestrator/test_task_complexity.py` | Unit TDD tests for the pure classifier (no LLM, no graph). | Create |
| `tests/services/orchestrator/features/conditional_gates.feature` | Gherkin behavior spec (content above). | Create |
| `tests/services/orchestrator/test_conditional_gates_bdd.py` | pytest-bdd step defs binding the feature to the classifier + routers. | Create |
| `tests/services/orchestrator/test_conditional_gates_wiring.py` | Unit tests for the graph wire-in (assess node skip path, both routers, flag-off regression). | Create |

---

## Task 1: Complexity classifier module (pure + deterministic)

**Files:**
- Create: `services/orchestrator/task_complexity.py`
- Test: `tests/services/orchestrator/test_task_complexity.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) Complexity` with fields `skip_ambiguity: bool`, `skip_verify: bool`, `reason: str`.
  - `conditional_gates_enabled() -> bool` — reads `ENABLE_CONDITIONAL_GATES` env (OFF by default).
  - `classify_complexity(task: str, *, enabled: bool | None = None) -> Complexity` — pure; when `enabled` is `None` it consults `conditional_gates_enabled()`. Returns `Complexity(False, False, "feature disabled")` when not enabled. Otherwise applies the deterministic heuristic.
- Consumes: nothing (stdlib `os`, `re`, `dataclasses` only).

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/orchestrator/test_task_complexity.py
"""Unit tests for the pure, deterministic complexity classifier.

No LLM, no graph, no env mutation beyond monkeypatched os.getenv. The classifier
is the ONLY place the skip decision is made, so these tests pin every branch.
"""
from __future__ import annotations

import pytest

from services.orchestrator.task_complexity import (
    Complexity,
    classify_complexity,
    conditional_gates_enabled,
)


@pytest.mark.mocked
class TestComplexityDataclass:
    def test_is_frozen_and_has_fields(self):
        c = Complexity(skip_ambiguity=True, skip_verify=True, reason="trivial")
        assert c.skip_ambiguity is True
        assert c.skip_verify is True
        assert c.reason == "trivial"
        with pytest.raises(Exception):
            c.skip_ambiguity = False  # frozen


@pytest.mark.mocked
class TestFeatureFlag:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("ENABLE_CONDITIONAL_GATES", raising=False)
        assert conditional_gates_enabled() is False

    @pytest.mark.parametrize("val", ["0", "false", "False", ""])
    def test_falsey_values_are_off(self, monkeypatch, val):
        monkeypatch.setenv("ENABLE_CONDITIONAL_GATES", val)
        assert conditional_gates_enabled() is False

    @pytest.mark.parametrize("val", ["1", "true", "True", "yes"])
    def test_truthy_values_are_on(self, monkeypatch, val):
        monkeypatch.setenv("ENABLE_CONDITIONAL_GATES", val)
        assert conditional_gates_enabled() is True


@pytest.mark.mocked
class TestClassifyDisabled:
    def test_disabled_never_skips(self, monkeypatch):
        monkeypatch.delenv("ENABLE_CONDITIONAL_GATES", raising=False)
        c = classify_complexity("What is 2+2?")
        assert c == Complexity(skip_ambiguity=False, skip_verify=False, reason="feature disabled")

    def test_explicit_enabled_false_overrides_env(self, monkeypatch):
        monkeypatch.setenv("ENABLE_CONDITIONAL_GATES", "1")
        c = classify_complexity("What is 2+2?", enabled=False)
        assert c.skip_ambiguity is False
        assert c.skip_verify is False


@pytest.mark.mocked
class TestClassifyTrivial:
    """When enabled, clearly trivial tasks skip both gates."""

    @pytest.mark.parametrize(
        "task",
        [
            "What is 2+2?",
            "what is the capital of France",
            "Who wrote Hamlet?",
            "Define entropy.",
            "Convert 10 km to miles",
        ],
    )
    def test_trivial_question_skips_both(self, task):
        c = classify_complexity(task, enabled=True)
        assert c.skip_ambiguity is True
        assert c.skip_verify is True
        assert c.reason  # non-empty explanation


@pytest.mark.mocked
class TestClassifyAmbiguous:
    """Underspecified phrasings must NEVER be classified trivial."""

    @pytest.mark.parametrize(
        "task",
        ["make it better", "fix the thing", "improve this", "do that", "handle it"],
    )
    def test_ambiguous_does_not_skip_ambiguity(self, task):
        c = classify_complexity(task, enabled=True)
        assert c.skip_ambiguity is False


@pytest.mark.mocked
class TestClassifyNonTrivial:
    """Long / multi-clause / build-y tasks keep both gates."""

    @pytest.mark.parametrize(
        "task",
        [
            "Implement a rate limiter with a sliding window and tests",
            "Write a Python module that parses CSV, validates rows, and writes JSON",
            "Refactor the auth layer to support OAuth and add integration tests",
            "Build a REST API with three endpoints and a Postgres schema",
        ],
    )
    def test_nontrivial_keeps_both_gates(self, task):
        c = classify_complexity(task, enabled=True)
        assert c.skip_ambiguity is False
        assert c.skip_verify is False


@pytest.mark.mocked
class TestDeterminism:
    def test_same_input_same_output(self):
        a = classify_complexity("What is 2+2?", enabled=True)
        b = classify_complexity("What is 2+2?", enabled=True)
        assert a == b

    def test_empty_string_is_safe_no_skip(self):
        c = classify_complexity("", enabled=True)
        assert c.skip_ambiguity is False
        assert c.skip_verify is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/services/orchestrator/test_task_complexity.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.orchestrator.task_complexity'`

- [ ] **Step 3: Write the classifier**

```python
# services/orchestrator/task_complexity.py
"""Pure, deterministic, LLM-free task-complexity classifier.

The orchestrator runs an LLM `assess_ambiguity` gate on EVERY task and a
`critique` (verify) gate on EVERY code/writing artifact. Both are wasted
latency on clearly trivial inputs (e.g. "What is 2+2?"). This module makes a
cheap, deterministic decision about whether a task is trivial enough that those
gates can be safely skipped.

Design rules (see the implementation plan's Global Constraints):
  * PURE: no network, no model calls, no clock, no randomness, no I/O.
  * CONSERVATIVE: only skip when the task is CLEARLY trivial. A false "skip"
    is worse than a false "don't skip" (skipping a genuinely-ambiguous task
    would let the agent guess), so every ambiguity signal blocks skip_ambiguity.
  * OFF BY DEFAULT: `ENABLE_CONDITIONAL_GATES` is unset/falsey -> never skip.

Relationship to the direct-answer fast-path: a skill-less single-intent task
(the fast-path's trigger) is the canonical "trivial" notion. This classifier is
a deterministic, conservative pre-filter layered on top: it skips the ambiguity
gate only for short, clearly-scoped question/lookup phrasings that the ambiguity
model would itself score ~0.0-0.1.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Complexity:
    """Deterministic verdict for a single task.

    skip_ambiguity: skip the LLM assess_ambiguity gate (task is clearly specified).
    skip_verify:    skip the critique/verify gate (task won't produce a risky artifact).
    reason:         short human-readable explanation (for events / debugging).
    """
    skip_ambiguity: bool
    skip_verify: bool
    reason: str


# Master flag. OFF by default. Matches the ENABLE_DIRECT_ANSWER_FASTPATH convention:
# a value in the falsey set means OFF; anything else means ON.
_FALSEY = ("0", "false", "False", "")


def conditional_gates_enabled() -> bool:
    """True when ENABLE_CONDITIONAL_GATES is set to a non-falsey value. OFF by default."""
    return os.getenv("ENABLE_CONDITIONAL_GATES", "0") not in _FALSEY


# Max word count for a task to even be CONSIDERED trivial. Short tasks only.
# Configurable for tuning; conservative default. Read at call time so tests/env
# can override without reimporting the module.
def _trivial_max_words() -> int:
    try:
        return int(os.getenv("TRIVIAL_MAX_WORDS", "12"))
    except (TypeError, ValueError):
        return 12


# Phrasings with an undefined referent / no concrete deliverable. If ANY of these
# match, the task is treated as potentially ambiguous and skip_ambiguity is False.
# Mirrors the assess_ambiguity prompt's HIGH-score triggers (undefined "it"/"the
# thing"/"this", vague "make it better" verbs).
_AMBIGUOUS_PATTERNS = (
    re.compile(r"\b(make|fix|improve|handle|do|change|update|refactor)\s+(it|this|that|the\s+thing)\b", re.I),
    re.compile(r"^\s*(make|fix|improve|handle|do|change)\s+(it|this|that)\s*$", re.I),
    re.compile(r"\bthe\s+thing\b", re.I),
)

# Signals that a task will likely produce a code/writing ARTIFACT that warrants the
# verify gate. If ANY match, skip_verify is False.
_ARTIFACT_PATTERNS = (
    re.compile(r"\b(write|implement|build|create|refactor|generate|draft|code|design)\b", re.I),
    re.compile(r"\b(function|module|class|api|endpoint|script|schema|test|tests|component|essay|paper|report)\b", re.I),
    re.compile(r"```"),  # the task itself contains a code block
)

# Signals that a task is a TRIVIAL question / lookup / conversion — the only family
# we allow to skip the ambiguity gate. Must be short AND match one of these AND not
# trip an ambiguity pattern.
_TRIVIAL_PATTERNS = (
    re.compile(r"^\s*(what|who|when|where|which|how\s+(much|many|far|old))\b", re.I),
    re.compile(r"^\s*(define|explain|summarize|name|list|tell\s+me)\b", re.I),
    re.compile(r"^\s*convert\b", re.I),
    re.compile(r"^\s*\d+\s*[-+*/]\s*\d+", re.I),  # bare arithmetic
)


def classify_complexity(task: str, *, enabled: bool | None = None) -> Complexity:
    """Classify a task's complexity. Pure & deterministic.

    When `enabled` is None, the master env flag decides. When the feature is
    disabled, ALWAYS returns no-skip (regression-safe). When enabled, applies a
    conservative heuristic: only short, clearly-scoped question/lookup tasks with
    no ambiguity markers and no artifact markers skip the gates.
    """
    if enabled is None:
        enabled = conditional_gates_enabled()
    if not enabled:
        return Complexity(skip_ambiguity=False, skip_verify=False, reason="feature disabled")

    text = (task or "").strip()
    if not text:
        return Complexity(skip_ambiguity=False, skip_verify=False, reason="empty task")

    words = text.split()
    is_short = len(words) <= _trivial_max_words()
    is_ambiguous = any(p.search(text) for p in _AMBIGUOUS_PATTERNS)
    is_artifact = any(p.search(text) for p in _ARTIFACT_PATTERNS)
    looks_trivial = any(p.search(text) for p in _TRIVIAL_PATTERNS)

    # skip_ambiguity: only when clearly a short, well-scoped question/lookup with
    # NO ambiguity markers. Conservative: a single ambiguity signal blocks it.
    skip_ambiguity = is_short and looks_trivial and not is_ambiguous

    # skip_verify: only when the task is short and clearly NOT going to produce a
    # code/writing artifact worth critiquing. Ambiguity also blocks it (a vague
    # task might still produce an artifact we'd want verified).
    skip_verify = is_short and not is_artifact and not is_ambiguous

    if skip_ambiguity and skip_verify:
        reason = "trivial question/lookup: short, well-scoped, no artifact"
    elif skip_verify:
        reason = "short non-artifact task: verify gate not warranted"
    elif is_ambiguous:
        reason = "ambiguity markers present: gates required"
    elif is_artifact:
        reason = "artifact-producing task: verify gate required"
    else:
        reason = "not clearly trivial: gates required"

    return Complexity(skip_ambiguity=skip_ambiguity, skip_verify=skip_verify, reason=reason)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/services/orchestrator/test_task_complexity.py -v`
Expected: PASS (all classes/params green).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/task_complexity.py tests/services/orchestrator/test_task_complexity.py
git commit -m "feat(orchestrator): pure deterministic task-complexity classifier"
```

---

## Task 2: Additive State fields

**Files:**
- Modify: `services/orchestrator/types.py:36-69` (the `State` TypedDict body — add three fields)
- Test: covered indirectly by Task 3 wiring tests (TypedDict fields need no standalone runtime test; we assert presence by using them).

**Interfaces:**
- Produces: `State` gains `complexity: dict`, `skip_ambiguity: bool`, `skip_verify: bool` (all optional — `State` is `total=False`).
- Consumes: nothing.

- [ ] **Step 1: Add the fields to State**

Add these three lines inside the `State` TypedDict, immediately after the
`direct_answer` field (currently line 69, the last field before the closing of
the class). The block to insert:

```python
    # Conditional gates (skip ambiguity/verify for trivial tasks). Additive; all
    # default-absent (State is total=False). complexity stores the classifier's
    # serialized verdict for events/debugging; skip_* are the committed routing flags.
    complexity: dict                  # {"skip_ambiguity": bool, "skip_verify": bool, "reason": str}
    skip_ambiguity: bool              # committed by assess_ambiguity; read by ambiguity_router
    skip_verify: bool                 # committed by assess_ambiguity; read by verify_router
```

Resulting tail of the `State` class (for reference — do not duplicate existing lines):

```python
    # FIX 10: direct-answer fast-path
    direct_answer: bool               # True when the plan node answered a skill-less single intent directly
    # Conditional gates (skip ambiguity/verify for trivial tasks). Additive; all
    # default-absent (State is total=False). complexity stores the classifier's
    # serialized verdict for events/debugging; skip_* are the committed routing flags.
    complexity: dict                  # {"skip_ambiguity": bool, "skip_verify": bool, "reason": str}
    skip_ambiguity: bool              # committed by assess_ambiguity; read by ambiguity_router
    skip_verify: bool                 # committed by assess_ambiguity; read by verify_router
```

- [ ] **Step 2: Verify the module still imports**

Run: `python -c "from services.orchestrator.types import State; print('ok')"`
Expected: prints `ok` (no syntax error).

- [ ] **Step 3: Run the existing types/graph tests (no regressions)**

Run: `pytest tests/services/orchestrator/test_graph.py -q`
Expected: PASS — existing graph tests unaffected by the additive fields.

- [ ] **Step 4: Commit**

```bash
git add services/orchestrator/types.py
git commit -m "feat(orchestrator): additive State fields for conditional gates"
```

---

## Task 3: Wire classifier into assess node + both routers

**Files:**
- Modify: `services/orchestrator/graph.py`
  - imports near line 13 (after `from . import events`)
  - `assess_ambiguity` node (438-541): compute complexity, short-circuit on `skip_ambiguity`, always stash flags
  - `ambiguity_router` (644-649): honor committed `skip_ambiguity`
  - `verify_router` (652-665): honor committed `skip_verify`
- Test: `tests/services/orchestrator/test_conditional_gates_wiring.py` (create)

**Interfaces:**
- Consumes (from Task 1): `classify_complexity`, `Complexity`, `conditional_gates_enabled`.
- Consumes (from Task 2): `State["skip_ambiguity"]`, `State["skip_verify"]`, `State["complexity"]`.
- Produces: `assess_ambiguity` now returns `complexity`, `skip_ambiguity`, `skip_verify` in its delta. `ambiguity_router` returns `"plan"` when `skip_ambiguity` is truthy (before the ambiguity-threshold check). `verify_router` returns `"check"` when `skip_verify` is truthy (before the `_verify_reflect` / threshold checks).

- [ ] **Step 1: Write the failing wiring tests**

```python
# tests/services/orchestrator/test_conditional_gates_wiring.py
"""Graph wire-in for conditional gates: assess node skip path + both routers.

Mocked only. Proves BOTH branches: trivial tasks skip the gates, non-trivial /
ambiguous tasks remain gated, and the master flag OFF preserves today's behavior.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.orchestrator import events
from services.orchestrator.types import create_goal


class _FakeEmitter:
    def __init__(self):
        self.events = []

    async def emit(self, type: str, **fields):
        self.events.append((type, fields))


@pytest.fixture
def fake_emitter():
    emitter = _FakeEmitter()
    token = events.current_emitter.set(emitter)
    yield emitter
    events.current_emitter.reset(token)


def _make_state(**overrides) -> dict:
    tree: dict = {}
    create_goal(tree, "root", None, "top-level task")
    base = {
        "session_id": "test-cg-001",
        "goal_tree": tree,
        "current_goal_id": "root",
        "step_markers": {},
        "messages": [],
        "error": None,
        "root_goal": "top-level task",
    }
    base.update(overrides)
    return base


def _nodes(architect_return: str):
    from services.orchestrator.graph import make_nodes
    from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

    mock_orch = MagicMock(spec=CodingOrchestrator)
    mock_orch.architect = AsyncMock(return_value=architect_return)
    mock_async_orch = MagicMock(spec=AsyncOrchestrator)
    return make_nodes(mock_orch, mock_async_orch), mock_orch


# ── assess_ambiguity skip path (feature ON) ────────────────────────────────

@pytest.mark.mocked
@pytest.mark.asyncio
async def test_assess_skips_llm_for_trivial_task(monkeypatch, fake_emitter):
    """With the feature ON and a trivial task, assess_ambiguity must NOT call the
    LLM and must commit skip flags so the routers short-circuit."""
    monkeypatch.setenv("ENABLE_CONDITIONAL_GATES", "1")
    nodes, mock_orch = _nodes('{"assumptions": [], "ambiguity": 0.0, "blocking_question": ""}')
    assess = nodes[5]

    delta = await assess(_make_state(root_goal="What is 2+2?"))

    # The expensive architect() ambiguity call was skipped entirely.
    mock_orch.architect.assert_not_called()
    assert delta["skip_ambiguity"] is True
    assert delta["skip_verify"] is True
    assert delta["complexity"]["reason"]
    # Not flagged ambiguous, so the graph proceeds to plan.
    assert delta.get("awaiting_clarification") is not True
    assert delta.get("ambiguity", 0.0) == 0.0


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_assess_still_runs_llm_for_ambiguous_task(monkeypatch, fake_emitter):
    """A vague task is NOT trivial, so assess_ambiguity still calls the LLM and gates."""
    monkeypatch.setenv("ENABLE_CONDITIONAL_GATES", "1")
    nodes, mock_orch = _nodes(
        '{"assumptions": ["?"], "ambiguity": 0.85, "blocking_question": "What should I make better?"}'
    )
    assess = nodes[5]

    delta = await assess(_make_state(root_goal="make it better"))

    mock_orch.architect.assert_awaited_once()
    assert delta["skip_ambiguity"] is False
    assert delta["ambiguity"] == 0.85
    assert delta["awaiting_clarification"] is True


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_assess_still_runs_llm_for_nontrivial_artifact_task(monkeypatch, fake_emitter):
    """An artifact-producing task keeps verify; assess still runs the LLM gate."""
    monkeypatch.setenv("ENABLE_CONDITIONAL_GATES", "1")
    nodes, mock_orch = _nodes('{"assumptions": [], "ambiguity": 0.1, "blocking_question": ""}')
    assess = nodes[5]

    delta = await assess(
        _make_state(root_goal="Implement a rate limiter with a sliding window and tests")
    )

    mock_orch.architect.assert_awaited_once()
    assert delta["skip_verify"] is False
    assert delta["skip_ambiguity"] is False


# ── feature OFF = identical to today ───────────────────────────────────────

@pytest.mark.mocked
@pytest.mark.asyncio
async def test_assess_flag_off_always_runs_llm(monkeypatch, fake_emitter):
    """Master flag OFF: even a trivial task runs the LLM gate (regression-safe)."""
    monkeypatch.delenv("ENABLE_CONDITIONAL_GATES", raising=False)
    nodes, mock_orch = _nodes('{"assumptions": [], "ambiguity": 0.0, "blocking_question": ""}')
    assess = nodes[5]

    delta = await assess(_make_state(root_goal="What is 2+2?"))

    mock_orch.architect.assert_awaited_once()
    assert delta["skip_ambiguity"] is False
    assert delta["skip_verify"] is False


# ── ambiguity_router honors skip flag ──────────────────────────────────────

@pytest.mark.mocked
def test_ambiguity_router_skips_to_plan_when_flagged():
    from services.orchestrator.graph import ambiguity_router
    # Even an above-threshold ambiguity yields "plan" when skip_ambiguity is set,
    # because the classifier already certified the task as trivial/clear.
    assert ambiguity_router(_make_state(skip_ambiguity=True, ambiguity=0.9)) == "plan"


@pytest.mark.mocked
def test_ambiguity_router_unaffected_when_flag_absent():
    from services.orchestrator.graph import ambiguity_router
    from langgraph.graph import END
    assert ambiguity_router(_make_state(ambiguity=0.7)) == END
    assert ambiguity_router(_make_state(ambiguity=0.2)) == "plan"


# ── verify_router honors skip flag ─────────────────────────────────────────

@pytest.mark.mocked
def test_verify_router_skips_to_check_when_flagged():
    from services.orchestrator.graph import verify_router
    # A below-threshold score would normally reflect; skip_verify forces check.
    assert verify_router(_make_state(skip_verify=True, critique_score=0.1, _verify_reflect=True)) == "check"


@pytest.mark.mocked
def test_verify_router_unaffected_when_flag_absent():
    from services.orchestrator.graph import verify_router
    assert verify_router(_make_state(_verify_reflect=True)) == "reflect"
    assert verify_router(_make_state(_verify_reflect=False)) == "check"
    assert verify_router(_make_state(critique_score=0.5)) == "reflect"
```

- [ ] **Step 2: Run the wiring tests to verify they fail**

Run: `pytest tests/services/orchestrator/test_conditional_gates_wiring.py -v`
Expected: FAIL — `skip_ambiguity`/`skip_verify` never set by `assess_ambiguity`; routers don't read them (e.g. `test_ambiguity_router_skips_to_plan_when_flagged` asserts `"plan"` but gets `END`; `test_assess_skips_llm_for_trivial_task` fails because `architect` is still called).

- [ ] **Step 3: Add the classifier import to graph.py**

In `services/orchestrator/graph.py`, immediately after the existing line
`from . import events` (currently line 13), add:

```python
from .task_complexity import classify_complexity, conditional_gates_enabled
```

- [ ] **Step 4: Short-circuit the assess_ambiguity node**

In `assess_ambiguity` (currently starting at line 438), replace the node's
opening — from the `goal = state.get(...)` line down to **just before** the
`prompt = (` assignment — with the version below. This computes complexity
first and, when the classifier certifies the task trivial, returns WITHOUT the
LLM call. The rest of the node (the `prompt = (...)` block through `return
result`) is unchanged, except the final `result` dict gains the three new keys
(Step 5).

Replace this (lines 438-439):

```python
    async def assess_ambiguity(state: State) -> dict:
        goal = state.get("root_goal") or state["goal_tree"][state["current_goal_id"]]["description"]
```

with:

```python
    async def assess_ambiguity(state: State) -> dict:
        goal = state.get("root_goal") or state["goal_tree"][state["current_goal_id"]]["description"]

        # Conditional gates: a cheap deterministic classifier decides whether this
        # task is trivial enough to skip the (LLM) ambiguity gate and/or the verify
        # gate. OFF by default; conservative when on. When it certifies the task
        # trivial-and-clear (skip_ambiguity), we skip the architect() ambiguity call
        # entirely and proceed straight to plan. The skip flags are committed on the
        # delta so ambiguity_router / verify_router can short-circuit downstream.
        cx = classify_complexity(goal, enabled=conditional_gates_enabled())
        cx_dict = {
            "skip_ambiguity": cx.skip_ambiguity,
            "skip_verify": cx.skip_verify,
            "reason": cx.reason,
        }
        if cx.skip_ambiguity:
            await events.emit(
                "reasoning",
                node="assess_ambiguity",
                summary=f"ambiguity gate skipped (trivial); {cx.reason}",
                text="",
            )
            return {
                "root_goal": goal,
                "assumptions": [],
                "ambiguity": 0.0,
                "blocking_question": "",
                "complexity": cx_dict,
                "skip_ambiguity": True,
                "skip_verify": cx.skip_verify,
            }
```

- [ ] **Step 5: Stash the skip flags on the normal (non-skip) return path**

The node currently builds `result` (line 518) and may add clarification keys,
then `return result` (line 541). Add the three complexity keys to `result` so
the gated path ALSO commits the flags (so `verify_router` can still honor a
`skip_verify` even when the ambiguity gate ran).

Replace this block (lines 518-523):

```python
        result = {
            "root_goal": goal,
            "assumptions": assumptions,
            "ambiguity": ambiguity,
            "blocking_question": blocking_question,
        }
```

with:

```python
        result = {
            "root_goal": goal,
            "assumptions": assumptions,
            "ambiguity": ambiguity,
            "blocking_question": blocking_question,
            "complexity": cx_dict,
            "skip_ambiguity": cx.skip_ambiguity,  # False here (skip path returned early)
            "skip_verify": cx.skip_verify,
        }
```

- [ ] **Step 6: Honor skip_ambiguity in ambiguity_router**

Replace `ambiguity_router` (currently lines 644-649):

```python
def ambiguity_router(state: State) -> str:
    """A1: route after assess_ambiguity. On high ambiguity, HALT (END) so the agent
    asks the user a clarifying question instead of guessing; otherwise plan."""
    if float(state.get("ambiguity", 0.0)) >= AMBIGUITY_THRESHOLD:
        return END
    return "plan"
```

with:

```python
def ambiguity_router(state: State) -> str:
    """A1: route after assess_ambiguity. On high ambiguity, HALT (END) so the agent
    asks the user a clarifying question instead of guessing; otherwise plan.

    Conditional gates: when the complexity classifier certified this task as
    trivial-and-clear (skip_ambiguity), proceed straight to plan regardless of the
    ambiguity score — the assess node skipped the LLM gate and left ambiguity at
    its 0.0 default, so this is belt-and-suspenders for any externally-set state."""
    if state.get("skip_ambiguity"):
        return "plan"
    if float(state.get("ambiguity", 0.0)) >= AMBIGUITY_THRESHOLD:
        return END
    return "plan"
```

- [ ] **Step 7: Honor skip_verify in verify_router**

Replace `verify_router` (currently lines 652-665):

```python
def verify_router(state: State) -> str:
    """A2 / FIX 9: route after verify. The verify node decides (and commits) whether a
    reflect pass is warranted via `_verify_reflect`, which is True only when the artifact
    scored below CRITIQUE_THRESHOLD AND fewer than MAX_VERIFY_RETRIES verify->reflect passes
    have been taken. This router is a pure function of that committed flag, so the
    verify<->reflect loop is bounded (it cannot loop forever).

    Backward-compat: if `_verify_reflect` was never set by the verify node (e.g. a hand-built
    state in older tests), fall back to the original threshold comparison."""
    if "_verify_reflect" in state:
        return "reflect" if state.get("_verify_reflect") else "check"
    if float(state.get("critique_score", 1.0)) < CRITIQUE_THRESHOLD:
        return "reflect"
    return "check"
```

with:

```python
def verify_router(state: State) -> str:
    """A2 / FIX 9: route after verify. The verify node decides (and commits) whether a
    reflect pass is warranted via `_verify_reflect`, which is True only when the artifact
    scored below CRITIQUE_THRESHOLD AND fewer than MAX_VERIFY_RETRIES verify->reflect passes
    have been taken. This router is a pure function of that committed flag, so the
    verify<->reflect loop is bounded (it cannot loop forever).

    Conditional gates: when the complexity classifier certified this task as low-risk
    (skip_verify), proceed straight to check — never reflect-loop on a trivial task even
    if the local Q4 critic under-scored it.

    Backward-compat: if `_verify_reflect` was never set by the verify node (e.g. a hand-built
    state in older tests), fall back to the original threshold comparison."""
    if state.get("skip_verify"):
        return "check"
    if "_verify_reflect" in state:
        return "reflect" if state.get("_verify_reflect") else "check"
    if float(state.get("critique_score", 1.0)) < CRITIQUE_THRESHOLD:
        return "reflect"
    return "check"
```

- [ ] **Step 8: Run the wiring tests to verify they pass**

Run: `pytest tests/services/orchestrator/test_conditional_gates_wiring.py -v`
Expected: PASS (all tests green).

- [ ] **Step 9: Run the full orchestrator suite for regressions**

Run: `pytest tests/services/orchestrator/ -q`
Expected: PASS — existing `test_graph.py`, `test_ambiguity_clarification.py`,
and `test_direct_answer_fastpath.py` all still pass (the feature is OFF by
default, so those suites — which never set `ENABLE_CONDITIONAL_GATES` — see
`skip_ambiguity=False`/`skip_verify=False` and behave identically to today).

- [ ] **Step 10: Commit**

```bash
git add services/orchestrator/graph.py tests/services/orchestrator/test_conditional_gates_wiring.py
git commit -m "feat(orchestrator): conditional gates wire-in (assess node + routers)"
```

---

## Task 4: BDD feature file + step defs

**Files:**
- Create: `tests/services/orchestrator/features/conditional_gates.feature` (content in "Behavior (BDD)" above)
- Create: `tests/services/orchestrator/test_conditional_gates_bdd.py`

**Interfaces:**
- Consumes: `classify_complexity` / `Complexity` (Task 1), `ambiguity_router` / `verify_router` / `make_nodes` (Task 3), the `fake_model` respx fixture from `tests/conftest.py` (foundation plan).
- Produces: executable pytest-bdd scenarios binding the Gherkin to real classifier + router calls.

- [ ] **Step 1: Create the feature file**

Create `tests/services/orchestrator/features/conditional_gates.feature` with the
**exact** Gherkin content shown in the "Behavior (BDD) — Gherkin" section above.

- [ ] **Step 2: Write the step defs (failing — feature file binds before steps exist)**

```python
# tests/services/orchestrator/test_conditional_gates_bdd.py
"""pytest-bdd step defs for conditional_gates.feature.

Binds the Gherkin to the real classifier and the real graph routers. Mocked only;
no GPU, no services. The ambiguity gate's "still runs" assertions drive the real
assess_ambiguity node with a mocked architect() (no LLM)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_bdd import scenarios, given, when, then, parsers

from langgraph.graph import END
from services.orchestrator import events
from services.orchestrator.task_complexity import classify_complexity
from services.orchestrator.graph import make_nodes, ambiguity_router, verify_router
from services.orchestrator.types import create_goal
from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

scenarios("features/conditional_gates.feature")


@pytest.fixture
def ctx():
    """Mutable scenario context."""
    return {"enabled": True, "task": "", "complexity": None, "assess_delta": None}


class _FakeEmitter:
    async def emit(self, type: str, **fields):
        pass


@pytest.fixture(autouse=True)
def _silence_events():
    token = events.current_emitter.set(_FakeEmitter())
    yield
    events.current_emitter.reset(token)


# ── Given ──────────────────────────────────────────────────────────────────

@given("the conditional-gates feature is enabled")
def _enabled(ctx):
    ctx["enabled"] = True


@given("the conditional-gates feature is disabled")
def _disabled(ctx):
    ctx["enabled"] = False


@given(parsers.parse('a task "{task}"'))
def _task(ctx, task):
    ctx["task"] = task


# ── When ───────────────────────────────────────────────────────────────────

@when("the orchestrator classifies the task complexity")
def _classify(ctx):
    ctx["complexity"] = classify_complexity(ctx["task"], enabled=ctx["enabled"])


# ── Then: classifier verdict ───────────────────────────────────────────────

@then(parsers.parse("the classifier marks skip_ambiguity {flag:w}"))
def _check_skip_ambiguity(ctx, flag):
    assert ctx["complexity"].skip_ambiguity is (flag == "true")


@then(parsers.parse("the classifier marks skip_verify {flag:w}"))
def _check_skip_verify(ctx, flag):
    assert ctx["complexity"].skip_verify is (flag == "true")


# ── Then: router behavior ──────────────────────────────────────────────────

@then("the ambiguity gate is skipped so routing goes straight to plan")
def _ambiguity_skipped(ctx):
    state = {"skip_ambiguity": ctx["complexity"].skip_ambiguity, "ambiguity": 0.9}
    assert ambiguity_router(state) == "plan"


@then("the verify gate is skipped so routing goes straight to check")
def _verify_skipped(ctx):
    state = {"skip_verify": ctx["complexity"].skip_verify, "_verify_reflect": True}
    assert verify_router(state) == "check"


@then("the ambiguity gate still runs and produces an ambiguity score")
def _ambiguity_runs(ctx):
    # Drive the real assess_ambiguity node with a mocked architect() (no LLM).
    # The task is non-trivial / disabled, so skip_ambiguity is False and the node
    # makes its architect() call and returns an ambiguity score.
    mock_orch = MagicMock(spec=CodingOrchestrator)
    mock_orch.architect = AsyncMock(
        return_value='{"assumptions": [], "ambiguity": 0.5, "blocking_question": ""}'
    )
    mock_async = MagicMock(spec=AsyncOrchestrator)
    assess = make_nodes(mock_orch, mock_async)[5]

    tree: dict = {}
    create_goal(tree, "root", None, ctx["task"])
    state = {
        "session_id": "bdd",
        "goal_tree": tree,
        "current_goal_id": "root",
        "step_markers": {},
        "messages": [],
        "error": None,
        "root_goal": ctx["task"],
    }

    import asyncio

    delta = asyncio.get_event_loop().run_until_complete(assess(state))
    mock_orch.architect.assert_awaited_once()
    assert "ambiguity" in delta
    assert delta["skip_ambiguity"] is False


@then("the verify gate still runs and routes a low-scoring artifact to reflect")
def _verify_runs(ctx):
    # skip_verify is False, so a below-threshold score reflects (normal behavior).
    state = {"skip_verify": ctx["complexity"].skip_verify, "_verify_reflect": True}
    assert verify_router(state) == "reflect"
```

Note on the `enabled` flag in the disabled scenario: the `Background` enables
the feature for every scenario, and the disabled scenario then overrides it with
"the conditional-gates feature is disabled" before the task is set. Because
`classify_complexity` is called with the explicit `enabled=ctx["enabled"]`
kwarg, no environment mutation is needed and the scenarios stay hermetic.

- [ ] **Step 3: Run the BDD scenarios to verify they fail (then pass once steps resolve)**

Run: `pytest tests/services/orchestrator/test_conditional_gates_bdd.py -v`
Expected on first run BEFORE Task 1/Task 3 are merged: import/collection error
(`task_complexity` or router symbols missing). After Tasks 1–3 are merged: PASS —
all four scenarios green.

- [ ] **Step 4: Commit**

```bash
git add tests/services/orchestrator/features/conditional_gates.feature tests/services/orchestrator/test_conditional_gates_bdd.py
git commit -m "test(orchestrator): BDD coverage for conditional gates"
```

---

## Task 5: Full regression sweep + env-knob documentation

**Files:**
- Modify: `infrastructure/local/local.env` (document the new env knobs alongside the existing ones)
- Test: whole orchestrator suite

**Interfaces:**
- Consumes: everything from Tasks 1–4.
- Produces: documented `ENABLE_CONDITIONAL_GATES` and `TRIVIAL_MAX_WORDS` knobs.

- [ ] **Step 1: Run the full orchestrator test suite**

Run: `pytest tests/services/orchestrator/ -q`
Expected: PASS — no regressions. Confirms the OFF-by-default invariant: suites
that never set `ENABLE_CONDITIONAL_GATES` behave exactly as before.

- [ ] **Step 2: Run the targeted conditional-gates tests together**

Run: `pytest tests/services/orchestrator/test_task_complexity.py tests/services/orchestrator/test_conditional_gates_wiring.py tests/services/orchestrator/test_conditional_gates_bdd.py -v`
Expected: PASS (all three files green).

- [ ] **Step 3: Document the env knobs**

Append to `infrastructure/local/local.env` (near the other orchestrator knobs;
do not remove existing lines):

```bash
# Conditional gates: skip the LLM ambiguity gate and/or the verify/critique gate
# for clearly trivial, low-risk tasks (e.g. "What is 2+2?"). OFF by default —
# unset or set to 0/false to preserve current always-gated behavior.
export ENABLE_CONDITIONAL_GATES="0"
# Max word count for a task to be eligible as "trivial" (conservative; default 12).
export TRIVIAL_MAX_WORDS="12"
```

- [ ] **Step 4: Commit**

```bash
git add infrastructure/local/local.env
git commit -m "docs(orchestrator): document conditional-gates env knobs"
```

---

## Self-Review

**1. Spec coverage**

| Requirement | Task |
|-------------|------|
| Cheap, deterministic task-complexity classifier | Task 1 (`classify_complexity`, pure/LLM-free) |
| Skip ambiguity and/or verify for trivial/low-risk tasks | Task 3 (assess node skip + both routers) |
| Reuse the direct-answer trivial notion | Task 1 docstring/heuristic mirrors the skill-less single-intent + assess_ambiguity HIGH triggers; the trivial family is the question/lookup phrasing the fast-path answers directly |
| Thresholds env-configurable | `ENABLE_CONDITIONAL_GATES`, `TRIVIAL_MAX_WORDS` (Task 1; documented Task 5) |
| OFF-by-default-safe / conservative | `conditional_gates_enabled()` defaults OFF; ambiguity/artifact markers block skips (Task 1); flag-off regression test (Task 3 Step 1) |
| Pure + deterministic + independently unit-tested helper | Task 1 (`test_task_complexity.py`, determinism tests) |
| Gates still run for non-trivial — both branches proven | Task 3 (`test_assess_still_runs_llm_for_ambiguous_task`, `..._nontrivial_artifact_task`); Task 4 scenarios 2 & 3 |
| Additive State fields only | Task 2 (`complexity`, `skip_ambiguity`, `skip_verify`) |
| Regression-safe with feature disabled | Task 3 (`test_assess_flag_off_always_runs_llm`); Task 4 scenario 4; Task 5 full sweep |
| `conditional_gates.feature` with required 4 scenarios | Task 4 (trivial-skips-both, ambiguous-still-gated, code-artifact-still-verified, flag-off-unchanged) |
| pytest-bdd step defs | Task 4 (`test_conditional_gates_bdd.py`) |
| Unit TDD tests | Task 1 (`test_task_complexity.py`) |
| Graph wire-in with tests | Task 3 (`test_conditional_gates_wiring.py`) |

**2. Placeholder scan:** No TBD/TODO; every code step shows full code. No "add error handling"/"similar to Task N" left dangling — the empty-string and bad-flag branches are handled explicitly in `classify_complexity` and pinned by tests.

**3. Type consistency:**
- `Complexity(skip_ambiguity, skip_verify, reason)` — same field names everywhere (dataclass def, classifier returns, assess node `cx.skip_ambiguity`/`cx.skip_verify`/`cx.reason`, tests).
- `classify_complexity(task, *, enabled=None)` — signature identical in module, graph wire-in, and all test/step-def call sites.
- `conditional_gates_enabled()` — same name in module, graph import, and assess node.
- State keys `complexity` / `skip_ambiguity` / `skip_verify` — same in `types.py`, assess node returns, and both routers.
- Node index: tests fetch `make_nodes(...)[5]` for `assess_ambiguity` and `[6]` for `verify`, matching the existing return order `(plan, execute_node, check, reflect, approval, assess_ambiguity, verify)` — unchanged by this plan.

All consistent. Plan complete.
