# Stateful Reflection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the orchestrator's `reflect` node condition each retry on its own prior reflections for the same goal, so a goal that fails twice gets a *different*, escalating diagnosis instead of the same one.

**Architecture:** Today the `reflect` node in `services/orchestrator/graph.py` builds an identical prompt every attempt (goal description + goal error) and discards all prior reflections, so there is no learning across retries. We add a small pure helper `collect_prior_reflections(state, goal_id)` that reads the `messages` list (LangGraph `Annotated[list, add]`) and returns the prior reflection texts for *this* goal, in order, capped at the last N (default 3). To make per-goal attribution possible, the reflection message entries gain a `goal_id` field (the current entries have only `role`/`content`, so they cannot be attributed today). The `reflect` node then injects those prior reflections into the diagnosis prompt with an explicit instruction to NOT repeat a previously-tried fix and to escalate strategy. First attempt (no priors) keeps the original behaviour exactly.

**Tech Stack:** Python 3.11+, LangGraph `StateGraph`, pytest, pytest-asyncio, pytest-bdd, `respx` (mocks the llama.cpp OpenAI-compatible endpoint), `unittest.mock`.

## Global Constraints

- Do NOT modify `core/orchestrator.py` or `main.py` — they are the working M2 baseline.
- Token counting (if ever added) uses the Gemma SentencePiece tokenizer via `transformers.AutoTokenizer` — NEVER `tiktoken`. (No token counting is introduced by this plan.)
- Reasoning budget: every `architect()` call passes `thinking_budget` explicitly. The `reflect` node uses `thinking_budget=REFLECT_THINKING_BUDGET` (env `REFLECT_THINKING_BUDGET`, default `1500`). Do not change this default.
- State must stay JSON-serializable: no Python objects, datetimes, or clients in `State`. Reflection message entries are plain dicts of strings/ints only.
- The `messages` field is `Annotated[list, add]` — never mutate it in place; nodes return a `{"messages": [...]}` delta that the reducer appends.
- Cap on included prior reflections: **last 3** (most recent), preserving chronological order. Expose as module constant `MAX_PRIOR_REFLECTIONS = 3`.
- New tests are marked `@pytest.mark.mocked` (no GPU, runs in CI). The feature file is tagged `@mocked`.
- Assert on prompt structure/substrings, never on exact LLM output text.
- BDD foundation (assumed already landed by the foundation plan): `tests/conftest.py` provides a `fake_model` `respx` fixture and pytest-bdd is installed. This plan consumes them; it does not create them.

---

## File Map

| File | Create/Modify | Responsibility |
|------|---------------|----------------|
| `services/orchestrator/graph.py` | Modify | Add pure helper `collect_prior_reflections(...)` + module constant `MAX_PRIOR_REFLECTIONS`; change the `reflect` node to read priors, build the escalation prompt, and tag the emitted reflection message with `goal_id`. |
| `tests/services/orchestrator/test_reflection_history.py` | Create | Unit TDD tests for the pure helper (no LLM). |
| `tests/services/orchestrator/test_stateful_reflection_node.py` | Create | Node-level test using the `fake_model` respx fixture: asserts the second-attempt prompt contains the first reflection text and the "do not repeat" instruction; asserts first-attempt prompt does not. |
| `tests/services/orchestrator/features/stateful_reflection.feature` | Create | Gherkin behavior spec, `@mocked`. |
| `tests/services/orchestrator/test_stateful_reflection_bdd.py` | Create | pytest-bdd step definitions binding the feature to the helper + node. |

### Reflection message shape (contract used across all tasks)

The `reflect` node currently appends `{"role": "reflection", "content": <text>}`. This plan changes it to:

```python
{"role": "reflection", "goal_id": <goal id str>, "content": <text>}
```

`collect_prior_reflections` filters on `role == "reflection"` AND `goal_id == goal_id`. Entries without a `goal_id` key (legacy / other roles) are ignored — this keeps the helper backward-safe against any pre-existing message in a resumed checkpoint.

---

## Behavior (BDD) — Gherkin

Create `tests/services/orchestrator/features/stateful_reflection.feature`:

```gherkin
@mocked
Feature: Stateful reflection conditions retries on prior attempts
  The reflect node must learn across retries for the same goal. The first
  failure produces a plain diagnosis. A subsequent failure must feed the
  earlier reflection(s) back into the prompt with an instruction not to
  repeat a previously-tried fix, and must respect the cap on how many
  prior reflections are included.

  Scenario: First failure produces a plain diagnosis with no prior reflections
    Given a goal "g1" that has failed once with error "AssertionError: expected 4 got 5"
    And the state has no prior reflections for goal "g1"
    When the reflect node runs
    Then the diagnosis prompt does not contain a "previously tried" section
    And the emitted reflection message is tagged with goal_id "g1"

  Scenario: Second failure feeds the first reflection back and forbids repeating it
    Given a goal "g1" that has failed twice with error "AssertionError: still wrong"
    And a prior reflection for goal "g1" saying "Try converting the input to int before adding"
    When the reflect node runs
    Then the diagnosis prompt contains "Try converting the input to int before adding"
    And the diagnosis prompt instructs the model not to repeat a previously-tried fix
    And the emitted reflection message is tagged with goal_id "g1"

  Scenario: Only the most recent reflections are included up to the cap
    Given a goal "g1" with 5 prior reflections numbered "fix 1" through "fix 5"
    When prior reflections are collected for goal "g1"
    Then exactly 3 reflections are returned
    And they are "fix 3", "fix 4", and "fix 5" in that order

  Scenario: Reflections for other goals are excluded
    Given a prior reflection "other-goal idea" for goal "g2"
    And a prior reflection "this-goal idea" for goal "g1"
    When prior reflections are collected for goal "g1"
    Then exactly 1 reflection is returned
    And it is "this-goal idea"
```

---

## Task 1: Pure helper `collect_prior_reflections` + unit tests

**Files:**
- Modify: `services/orchestrator/graph.py` (add module constant + helper near `classify_artifact`, around line 104–114)
- Test: `tests/services/orchestrator/test_reflection_history.py`

**Interfaces:**
- Consumes: `State` (`services.orchestrator.types`) — specifically its `messages` list.
- Produces:
  - Module constant `MAX_PRIOR_REFLECTIONS: int = 3`
  - `collect_prior_reflections(state: State, goal_id: str, cap: int = MAX_PRIOR_REFLECTIONS) -> list[str]`
    Returns the `content` strings of reflection messages whose `goal_id` matches, in original (chronological) order, keeping only the **last `cap`** of them.

- [ ] **Step 1: Write the failing tests**

Create `tests/services/orchestrator/test_reflection_history.py`:

```python
"""Unit tests for collect_prior_reflections — pure, no LLM, no services."""
from __future__ import annotations

import pytest

from services.orchestrator.graph import (
    collect_prior_reflections,
    MAX_PRIOR_REFLECTIONS,
)


def _state(messages):
    # Minimal State-shaped dict; only `messages` is read by the helper.
    return {"messages": messages}


class TestCollectPriorReflections:
    def test_empty_messages_returns_empty_list(self):
        assert collect_prior_reflections(_state([]), "g1") == []

    def test_missing_messages_key_returns_empty_list(self):
        # Resumed checkpoints may not carry `messages`; must not raise.
        assert collect_prior_reflections({}, "g1") == []

    def test_returns_only_matching_goal_id_in_order(self):
        messages = [
            {"role": "reflection", "goal_id": "g1", "content": "first"},
            {"role": "reflection", "goal_id": "g2", "content": "other"},
            {"role": "reflection", "goal_id": "g1", "content": "second"},
        ]
        assert collect_prior_reflections(_state(messages), "g1") == ["first", "second"]

    def test_ignores_non_reflection_roles(self):
        messages = [
            {"role": "user", "goal_id": "g1", "content": "noise"},
            {"role": "reflection", "goal_id": "g1", "content": "keep"},
        ]
        assert collect_prior_reflections(_state(messages), "g1") == ["keep"]

    def test_ignores_legacy_entries_without_goal_id(self):
        # Pre-change reflections had no goal_id; they must be skipped, not crash.
        messages = [
            {"role": "reflection", "content": "legacy-no-goal-id"},
            {"role": "reflection", "goal_id": "g1", "content": "tagged"},
        ]
        assert collect_prior_reflections(_state(messages), "g1") == ["tagged"]

    def test_caps_to_last_n_preserving_order(self):
        messages = [
            {"role": "reflection", "goal_id": "g1", "content": f"fix {i}"}
            for i in range(1, 6)  # fix 1 .. fix 5
        ]
        out = collect_prior_reflections(_state(messages), "g1")
        assert out == ["fix 3", "fix 4", "fix 5"]
        assert len(out) == MAX_PRIOR_REFLECTIONS

    def test_explicit_cap_argument_overrides_default(self):
        messages = [
            {"role": "reflection", "goal_id": "g1", "content": f"fix {i}"}
            for i in range(1, 6)
        ]
        assert collect_prior_reflections(_state(messages), "g1", cap=2) == ["fix 4", "fix 5"]

    def test_cap_zero_returns_empty(self):
        messages = [{"role": "reflection", "goal_id": "g1", "content": "x"}]
        assert collect_prior_reflections(_state(messages), "g1", cap=0) == []

    def test_default_cap_is_three(self):
        assert MAX_PRIOR_REFLECTIONS == 3
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/services/orchestrator/test_reflection_history.py -v`
Expected: FAIL at import — `ImportError: cannot import name 'collect_prior_reflections' from 'services.orchestrator.graph'`.

- [ ] **Step 3: Implement the helper**

In `services/orchestrator/graph.py`, immediately after the `classify_artifact` function (currently ending at line 113) add:

```python
# Maximum number of prior reflections (most-recent) fed back into a retry's
# diagnosis prompt. Bounded so the prompt cannot grow unboundedly across
# many retries; deterministic ordering (chronological) for reproducibility.
MAX_PRIOR_REFLECTIONS = 3


def collect_prior_reflections(
    state: "State",
    goal_id: str,
    cap: int = MAX_PRIOR_REFLECTIONS,
) -> list[str]:
    """Return prior reflection texts recorded for `goal_id`, in chronological
    order, keeping only the last `cap`.

    Pure: reads only `state["messages"]`. Reflection messages are the dicts
    appended by the reflect node, shaped
    ``{"role": "reflection", "goal_id": <id>, "content": <text>}``.
    Entries that are not reflections, or that lack a matching ``goal_id``
    (e.g. legacy pre-change reflections, or reflections for other goals),
    are ignored. ``cap <= 0`` yields an empty list.
    """
    messages = state.get("messages") or []
    matches = [
        m["content"]
        for m in messages
        if isinstance(m, dict)
        and m.get("role") == "reflection"
        and m.get("goal_id") == goal_id
        and "content" in m
    ]
    if cap <= 0:
        return []
    return matches[-cap:]
```

Note: `State` is already imported/available in `graph.py` (used throughout the node signatures). If the file uses `from .types import State`, the string annotation `"State"` resolves fine; keep it as a string to avoid any ordering concerns.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/services/orchestrator/test_reflection_history.py -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/graph.py tests/services/orchestrator/test_reflection_history.py
git commit -m "feat(orchestrator): add collect_prior_reflections pure helper for stateful reflection

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Reflect node consumes prior reflections + tags message with goal_id

**Files:**
- Modify: `services/orchestrator/graph.py` — the `reflect` node (currently lines 391–418)
- Test: `tests/services/orchestrator/test_stateful_reflection_node.py`

**Interfaces:**
- Consumes: `collect_prior_reflections` and `MAX_PRIOR_REFLECTIONS` from Task 1; `orch.architect(prompt, thinking_budget=...)`; `REFLECT_THINKING_BUDGET`.
- Produces: the `reflect` node now (a) builds the diagnosis prompt including a "Previously tried (do NOT repeat)" section when priors exist, and (b) returns a reflection message tagged with `goal_id`:
  `{"role": "reflection", "goal_id": gid, "content": reflection}`.

- [ ] **Step 1: Write the failing test**

Create `tests/services/orchestrator/test_stateful_reflection_node.py`:

```python
"""Node-level tests for the stateful reflect node.

Drives the real reflect node with a mocked orchestrator (architect captured),
asserting the prompt is conditioned on prior reflections on retry and not on
the first attempt. No GPU / no live services.
"""
from __future__ import annotations

import pytest
from contextvars import copy_context
from unittest.mock import AsyncMock, MagicMock

from services.orchestrator import events
from services.orchestrator.graph import make_nodes
from services.orchestrator.coding_orchestrator import (
    CodingOrchestrator,
    AsyncOrchestrator,
)
from services.orchestrator.types import Status, create_goal


pytestmark = pytest.mark.mocked


def _make_reflect_node(architect_return="DIAGNOSIS-OUT"):
    mock_orch = MagicMock(spec=CodingOrchestrator)
    mock_orch.architect = AsyncMock(return_value=architect_return)
    mock_async_orch = MagicMock(spec=AsyncOrchestrator)
    # make_nodes returns: plan, execute_node, check, reflect, approval,
    # assess_ambiguity, verify  (7 nodes — see CLAUDE.md make_nodes arity note)
    plan, execute_node, check, reflect, approval, assess_ambiguity, verify = make_nodes(
        mock_orch, mock_async_orch
    )
    return reflect, mock_orch


def _state_with_failed_goal(gid="g1", error="AssertionError: expected 4 got 5",
                            attempts=1, messages=None):
    tree = {}
    create_goal(tree, gid, None, "Compute 2+2")
    tree[gid]["status"] = Status.FAILED.value
    tree[gid]["error"] = error
    tree[gid]["attempts"] = attempts
    return {
        "session_id": "s1",
        "current_goal_id": gid,
        "goal_tree": tree,
        "messages": messages or [],
    }


async def _run_node(node, state):
    # events uses a ContextVar emitter; run inside a fresh context with a noop emitter.
    noop = AsyncMock()
    emitter = MagicMock()
    emitter.emit = noop

    async def _call():
        token = events._current_emitter.set(emitter) if hasattr(events, "_current_emitter") else None
        try:
            return await node(state)
        finally:
            if token is not None:
                events._current_emitter.reset(token)

    return await copy_context().run(lambda: _call())


@pytest.mark.asyncio
async def test_first_attempt_prompt_has_no_prior_section():
    reflect, mock_orch = _make_reflect_node()
    state = _state_with_failed_goal(attempts=1, messages=[])
    await _run_node(reflect, state)

    prompt = mock_orch.architect.call_args.args[0]
    assert "Previously tried" not in prompt
    assert "do NOT repeat" not in prompt
    # Original behavior preserved: goal + error still present.
    assert "Compute 2+2" in prompt
    assert "AssertionError: expected 4 got 5" in prompt


@pytest.mark.asyncio
async def test_first_attempt_message_tagged_with_goal_id():
    reflect, mock_orch = _make_reflect_node(architect_return="fresh diagnosis")
    state = _state_with_failed_goal(gid="g1", attempts=1, messages=[])
    out = await _run_node(reflect, state)

    msgs = out["messages"]
    assert len(msgs) == 1
    assert msgs[0]["role"] == "reflection"
    assert msgs[0]["goal_id"] == "g1"
    assert msgs[0]["content"] == "fresh diagnosis"


@pytest.mark.asyncio
async def test_second_attempt_prompt_includes_prior_reflection_and_forbids_repeat():
    reflect, mock_orch = _make_reflect_node()
    prior = [
        {"role": "reflection", "goal_id": "g1",
         "content": "Try converting the input to int before adding"},
    ]
    state = _state_with_failed_goal(
        gid="g1", error="AssertionError: still wrong", attempts=2, messages=prior
    )
    await _run_node(reflect, state)

    prompt = mock_orch.architect.call_args.args[0]
    assert "Try converting the input to int before adding" in prompt
    assert "Previously tried" in prompt
    assert "do NOT repeat" in prompt


@pytest.mark.asyncio
async def test_second_attempt_excludes_other_goals_reflections():
    reflect, mock_orch = _make_reflect_node()
    msgs = [
        {"role": "reflection", "goal_id": "OTHER", "content": "irrelevant other-goal fix"},
        {"role": "reflection", "goal_id": "g1", "content": "relevant g1 fix"},
    ]
    state = _state_with_failed_goal(gid="g1", attempts=2, messages=msgs)
    await _run_node(reflect, state)

    prompt = mock_orch.architect.call_args.args[0]
    assert "relevant g1 fix" in prompt
    assert "irrelevant other-goal fix" not in prompt


@pytest.mark.asyncio
async def test_reflect_uses_reflect_thinking_budget():
    from services.orchestrator.graph import REFLECT_THINKING_BUDGET

    reflect, mock_orch = _make_reflect_node()
    state = _state_with_failed_goal(attempts=1, messages=[])
    await _run_node(reflect, state)

    assert mock_orch.architect.call_args.kwargs["thinking_budget"] == REFLECT_THINKING_BUDGET
```

> Note for the implementer: the `_run_node` helper sets the events emitter ContextVar so `await events.emit(...)` inside the node does not raise. If `services/orchestrator/events.py` exposes a different setter (e.g. `events.set_emitter(...)` or a `EventEmitter` context manager), mirror the pattern already used in `tests/services/orchestrator/test_graph.py` for nodes that emit — copy that exact setup rather than inventing one. Verify by opening `test_graph.py` and reusing its emitter fixture if present.

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/services/orchestrator/test_stateful_reflection_node.py -v`
Expected: FAIL — `test_second_attempt_prompt_includes_prior_reflection_and_forbids_repeat` fails because the current prompt never includes priors; `test_*_tagged_with_goal_id` fails because the current message has no `goal_id` key.

- [ ] **Step 3: Edit the reflect node**

In `services/orchestrator/graph.py`, replace the body of the `reflect` node (currently lines 391–418) with:

```python
    async def reflect(state: State) -> dict:
        """
        Reflexion: write a natural-language diagnosis to episodic memory.
        Conditions the next execute attempt on the stored reflection AND on
        any prior reflections for THIS goal, so retries escalate strategy
        instead of repeating the same diagnosis.
        """
        import copy
        gid = state["current_goal_id"]
        goal = state["goal_tree"][gid]

        priors = collect_prior_reflections(state, gid)
        prior_section = ""
        if priors:
            numbered = "\n".join(f"  {i}. {p}" for i, p in enumerate(priors, 1))
            prior_section = (
                "\nPreviously tried (do NOT repeat these fixes — they already failed):\n"
                f"{numbered}\n"
                "Propose a DIFFERENT approach and escalate your strategy.\n"
            )

        reflection = await orch.architect(
            f"The following subtask failed (attempt {goal['attempts']}):\n"
            f"Goal: {goal['description']}\n"
            f"Error: {goal['error']}\n"
            f"{prior_section}"
            "Write a concise diagnosis and what to do differently on the next attempt.",
            thinking_budget=REFLECT_THINKING_BUDGET,  # FIX 9: was 3000
        )
        await events.emit(
            "reasoning",
            node="reflect",
            summary="diagnosing failed subtask",
            text=reflection[:500],
        )
        # Deep copy to avoid mutating the checkpoint's prior goal_tree.
        tree = copy.deepcopy(state["goal_tree"])
        update_status(tree, gid, Status.PENDING)
        return {
            "goal_tree": tree,
            # Tag with goal_id so collect_prior_reflections can attribute this
            # reflection on the next retry of the same goal.
            "messages": [{"role": "reflection", "goal_id": gid, "content": reflection}],
        }
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/services/orchestrator/test_stateful_reflection_node.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Run the orchestrator regression suite**

Run: `pytest tests/services/orchestrator/test_graph.py tests/services/orchestrator/test_graph_fix9_loop_bounding.py tests/services/orchestrator/test_reflection_history.py -v`
Expected: PASS — no regressions. (If any existing test asserts the exact reflection message dict equals `{"role": "reflection", "content": ...}` without a `goal_id`, update that assertion to include `"goal_id"`; grep first: `grep -rn '"role": "reflection"' tests/`.)

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/graph.py tests/services/orchestrator/test_stateful_reflection_node.py
git commit -m "feat(orchestrator): condition reflect node on prior reflections per goal

reflect now feeds the last N (=3) prior reflections for the same goal into
the diagnosis prompt with a do-not-repeat/escalate instruction, and tags
each reflection message with goal_id for attribution. First attempt keeps
original behavior.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: pytest-bdd step definitions binding the feature

**Files:**
- Create: `tests/services/orchestrator/test_stateful_reflection_bdd.py`
- Consumes: `tests/services/orchestrator/features/stateful_reflection.feature` (Gherkin from the "Behavior" section — create the `.feature` file as part of this task), the `fake_model` respx fixture from `tests/conftest.py`, `collect_prior_reflections`, `make_nodes`.

**Interfaces:**
- Consumes: `collect_prior_reflections`, `make_nodes`, `create_goal`, `Status`, and the `fake_model` fixture (foundation). The node scenarios use `fake_model` to make `orch.architect` reach the mocked llama.cpp endpoint; the helper-only scenarios call `collect_prior_reflections` directly.

- [ ] **Step 1: Create the feature file**

Create `tests/services/orchestrator/features/stateful_reflection.feature` with the exact Gherkin from the "Behavior (BDD) — Gherkin" section above.

- [ ] **Step 2: Write the failing step definitions**

Create `tests/services/orchestrator/test_stateful_reflection_bdd.py`:

```python
"""pytest-bdd step defs for stateful_reflection.feature."""
from __future__ import annotations

import pytest
from contextvars import copy_context
from unittest.mock import AsyncMock, MagicMock
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator import events
from services.orchestrator.graph import make_nodes, collect_prior_reflections
from services.orchestrator.coding_orchestrator import (
    CodingOrchestrator,
    AsyncOrchestrator,
)
from services.orchestrator.types import Status, create_goal

pytestmark = pytest.mark.mocked

scenarios("features/stateful_reflection.feature")


@pytest.fixture
def ctx():
    # Shared mutable context across Given/When/Then steps.
    return {
        "messages": [],
        "goal_id": "g1",
        "error": "AssertionError: expected 4 got 5",
        "attempts": 1,
        "captured_prompt": None,
        "node_output": None,
        "collected": None,
    }


def _build_reflect(architect_return="DIAGNOSIS-OUT"):
    mock_orch = MagicMock(spec=CodingOrchestrator)
    mock_orch.architect = AsyncMock(return_value=architect_return)
    mock_async_orch = MagicMock(spec=AsyncOrchestrator)
    nodes = make_nodes(mock_orch, mock_async_orch)
    reflect = nodes[3]  # plan, execute_node, check, reflect, ...
    return reflect, mock_orch


async def _run(node, state):
    emitter = MagicMock()
    emitter.emit = AsyncMock()

    async def _call():
        token = events._current_emitter.set(emitter) if hasattr(events, "_current_emitter") else None
        try:
            return await node(state)
        finally:
            if token is not None:
                events._current_emitter.reset(token)

    return await copy_context().run(lambda: _call())


def _state(ctx):
    tree = {}
    create_goal(tree, ctx["goal_id"], None, "Compute 2+2")
    tree[ctx["goal_id"]]["status"] = Status.FAILED.value
    tree[ctx["goal_id"]]["error"] = ctx["error"]
    tree[ctx["goal_id"]]["attempts"] = ctx["attempts"]
    return {
        "session_id": "s1",
        "current_goal_id": ctx["goal_id"],
        "goal_tree": tree,
        "messages": ctx["messages"],
    }


# ---- Given ---------------------------------------------------------------- #

@given(parsers.parse('a goal "{gid}" that has failed once with error "{error}"'))
def goal_failed_once(ctx, gid, error):
    ctx["goal_id"] = gid
    ctx["error"] = error
    ctx["attempts"] = 1


@given(parsers.parse('a goal "{gid}" that has failed twice with error "{error}"'))
def goal_failed_twice(ctx, gid, error):
    ctx["goal_id"] = gid
    ctx["error"] = error
    ctx["attempts"] = 2


@given(parsers.parse('the state has no prior reflections for goal "{gid}"'))
def no_prior_reflections(ctx, gid):
    ctx["messages"] = []


@given(parsers.parse('a prior reflection for goal "{gid}" saying "{text}"'))
def a_prior_reflection_saying(ctx, gid, text):
    ctx["messages"].append({"role": "reflection", "goal_id": gid, "content": text})


@given(parsers.parse('a prior reflection "{text}" for goal "{gid}"'))
def a_prior_reflection_for(ctx, text, gid):
    ctx["messages"].append({"role": "reflection", "goal_id": gid, "content": text})


@given(parsers.parse('a goal "{gid}" with 5 prior reflections numbered "{first}" through "{last}"'))
def five_prior_reflections(ctx, gid, first, last):
    ctx["goal_id"] = gid
    ctx["messages"] = [
        {"role": "reflection", "goal_id": gid, "content": f"fix {i}"}
        for i in range(1, 6)
    ]


# ---- When ----------------------------------------------------------------- #

@when("the reflect node runs", target_fixture="ran")
def reflect_runs(ctx):
    import asyncio
    reflect, mock_orch = _build_reflect()
    out = asyncio.get_event_loop().run_until_complete(_run(reflect, _state(ctx)))
    ctx["node_output"] = out
    ctx["captured_prompt"] = mock_orch.architect.call_args.args[0]
    return True


@when(parsers.parse('prior reflections are collected for goal "{gid}"'))
def collect_for_goal(ctx, gid):
    ctx["collected"] = collect_prior_reflections({"messages": ctx["messages"]}, gid)


# ---- Then ----------------------------------------------------------------- #

@then('the diagnosis prompt does not contain a "previously tried" section')
def prompt_no_prior_section(ctx):
    assert "Previously tried" not in ctx["captured_prompt"]


@then(parsers.parse('the diagnosis prompt contains "{text}"'))
def prompt_contains(ctx, text):
    assert text in ctx["captured_prompt"]


@then("the diagnosis prompt instructs the model not to repeat a previously-tried fix")
def prompt_forbids_repeat(ctx):
    assert "do NOT repeat" in ctx["captured_prompt"]


@then(parsers.parse('the emitted reflection message is tagged with goal_id "{gid}"'))
def message_tagged(ctx, gid):
    msgs = ctx["node_output"]["messages"]
    assert msgs[0]["role"] == "reflection"
    assert msgs[0]["goal_id"] == gid


@then(parsers.parse("exactly {n:d} reflections are returned"))
def exactly_n_returned(ctx, n):
    assert len(ctx["collected"]) == n


@then(parsers.parse('they are "{a}", "{b}", and "{c}" in that order'))
def three_in_order(ctx, a, b, c):
    assert ctx["collected"] == [a, b, c]


@then(parsers.parse('it is "{text}"'))
def single_value_is(ctx, text):
    assert ctx["collected"] == [text]
```

> Implementer note: if the foundation `conftest.py` exposes a different async-run convention (e.g. an `event_loop` fixture or `pytest.mark.asyncio` BDD support), prefer that over `asyncio.get_event_loop().run_until_complete` in the `When` step — mirror whatever the other `*_bdd.py` files in the repo do. The behavior asserted is unchanged.

- [ ] **Step 3: Run the BDD tests to verify they fail (then pass)**

First confirm collection works and scenarios are bound:
Run: `pytest tests/services/orchestrator/test_stateful_reflection_bdd.py -v`
Expected before Task 2 is merged: the two node scenarios FAIL (no prior section / no goal_id tag). After Task 2 is merged, expected: PASS (4 scenarios).

Since Task 2 lands before this task, expected here: PASS (4 scenarios).

- [ ] **Step 4: Commit**

```bash
git add tests/services/orchestrator/features/stateful_reflection.feature tests/services/orchestrator/test_stateful_reflection_bdd.py
git commit -m "test(orchestrator): BDD feature + step defs for stateful reflection

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Full regression sweep

**Files:** none (verification only)

- [ ] **Step 1: Run the full orchestrator test suite**

Run: `pytest tests/services/orchestrator/ -v 2>&1 | tail -30`
Expected: all pass; specifically the existing 342 orchestrator tests plus the new files. No test that previously asserted the bare reflection-message shape should remain red — if one is, it must have been updated in Task 2 Step 5.

- [ ] **Step 2: Confirm no stray `tiktoken` / forbidden patterns were introduced**

Run: `grep -rn "tiktoken" services/orchestrator/graph.py tests/services/orchestrator/test_reflection_history.py tests/services/orchestrator/test_stateful_reflection_node.py tests/services/orchestrator/test_stateful_reflection_bdd.py`
Expected: no output.

- [ ] **Step 3: Commit (only if any fixups were needed)**

```bash
git add -A
git commit -m "test(orchestrator): regression fixups for stateful reflection

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

(If Step 1 was already green and nothing changed, skip this commit.)

---

## Self-Review

**1. Spec coverage**
- Pure, independently unit-testable helper `collect_prior_reflections(state, goal_id) -> list[str]` — Task 1. ✓
- Helper is pure, no LLM — Task 1 tests call it with plain dicts. ✓
- Reflect node still works on first attempt (no priors → original behavior) — Task 2 `test_first_attempt_prompt_has_no_prior_section` asserts no prior section and that goal+error remain; the prompt body is byte-identical to the original when `prior_section == ""`. ✓
- Second failure includes the first reflection + "do not repeat / escalate" instruction — Task 2 `test_second_attempt_prompt_includes_prior_reflection_and_forbids_repeat` + BDD scenario 2. ✓
- Bounded (cap last 3) and deterministic ordering — `MAX_PRIOR_REFLECTIONS = 3`, `matches[-cap:]` preserves chronological order; Task 1 `test_caps_to_last_n_preserving_order` + BDD scenario 3. ✓
- Additive State/Goal fields only if needed — no new `State`/`Goal` TypedDict fields added; we derive from existing `messages` and only enrich the reflection message dict with a `goal_id` key (the message list is untyped `Annotated[list, add]`, so no type change). ✓
- Regression-safe — Task 2 Step 5 + Task 4 full sweep; Task 2 Step 5 explicitly greps for and fixes any test asserting the old reflection-message shape. ✓
- Feature file `tests/services/orchestrator/features/stateful_reflection.feature`, `@mocked` — Task 3 Step 1. ✓
- Step defs `tests/services/orchestrator/test_stateful_reflection_bdd.py` (pytest_bdd) — Task 3. ✓
- Unit TDD tests `tests/services/orchestrator/test_reflection_history.py` — Task 1. ✓

**2. Placeholder scan** — No TBD/TODO; every code step shows full code; commands have explicit expected output. The only deliberate "mirror the repo convention" notes (events emitter setter, BDD async-run) are because those belong to the assumed foundation plan and the repo's existing test files; each gives a concrete fallback so the task is executable as written.

**3. Type consistency** — `collect_prior_reflections(state, goal_id, cap=MAX_PRIOR_REFLECTIONS) -> list[str]` and `MAX_PRIOR_REFLECTIONS = 3` are used identically in graph.py, the helper unit tests, the node test, and the BDD step defs. The reflection message shape `{"role": "reflection", "goal_id": gid, "content": reflection}` is identical in the node (Task 2), the helper filter (Task 1), and every test fixture. `make_nodes` is unpacked as 7 nodes everywhere (`reflect` is index 3), matching the CLAUDE.md arity note.
