# Revise-Before-Deliver (lightweight, opt-in) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in, bounded single-pass gate that re-reads the final user-facing answer against the original task and may produce ONE revised answer before delivery — catching incompleteness, unsupported claims, and fabrication.

**Architecture:** A new pure-helper module (`finalize_revision.py`) holds the side-effect/cap/empty/error guard (`should_revise`) and the prompt builder (`build_revision_prompt`). A new **`revise` graph node** is inserted between `check` and `END` (chosen seam — see below). `check` stays deterministic; the `revise` node makes at most `MAX_FINALIZE_REVISIONS` model calls (default 1) and overwrites `final_answer` in place. Default OFF (`ENABLE_FINALIZE_REVISION=0`) makes the new node a pass-through, so the graph and delivery are byte-identical to today when disabled.

**Tech Stack:** Python 3.11, asyncio, LangGraph `StateGraph`, litellm via `orch.architect`, pytest + pytest-asyncio + pytest-bdd (respx `fake_model` seam).

## Chosen Seam — node vs. stream_final_answer

**Chosen: a new `revise` graph node between `check` and `END`.** Justification:

1. **Keeps `check` deterministic.** `check` builds `final_answer` from the goal tree with no model call; folding a revision model call into `check` would make a deterministic finalizer non-deterministic and break the many existing `check` unit tests that assert exact assembled text.
2. **One authoritative `final_answer`.** Both delivery paths in `main._handle` read `final_state["final_answer"]` (the `direct_answer` path, and the `stream_final_answer` default path which seeds its prompt from `assembled = final_state["final_answer"]`). Revising in the graph means *every* delivery path inherits the revised answer with **zero changes to `main.py`** — the revised text simply flows through `stream_final_answer` (which re-paraphrases for the typewriter effect) or is emitted directly. Folding into `stream_final_answer` instead would (a) miss the `direct_answer` and `awaiting_clarification` paths, and (b) entangle the revision logic with streaming/typewriter concerns.
3. **The graph already owns `final_answer`'s lifecycle** (`check` sets it; `router` treats it as terminal). The revision belongs at the same layer.
4. **The side-effect guard needs graph state** (`last_artifact`, `error`, `error_class`, goal tree) which is fully present at the `check → END` boundary and only partially reconstructable in `stream_final_answer`.

The `revise` node runs only after `check` finalized (`final_answer` set, no retryable failures left). Because `router` currently routes `check → END` when `final_answer` is set, we re-point that edge through `revise`.

## Global Constraints

Copied verbatim from CLAUDE.md — every task's requirements implicitly include these:

- **stdout is sacred:** never `print()` / `console.log` in orchestrator code; logging to stderr only (`logging`).
- **Every llama.cpp call sets `thinking_budget_tokens`** via `extra_body` (post-April-2026 builds default to `INT_MAX` → hangs). The revision call reuses `orch.architect(prompt, thinking_budget=...)`, which already sets it.
- **Every `litellm.acompletion` sets `api_key="not-needed"`.** `orch.architect` already does; the revision path goes through `architect`, so this is honored.
- **asyncio-correct:** no `asyncio.run()` inside async context; nodes are `async def`. `ClientSession` lifetime rules N/A (no new MCP session).
- **Python files `snake_case.py`; Python classes PascalCase; functions `snake_case`.**
- **Tests live under `tests/` mirroring `services/`; `@pytest.mark.asyncio` on async tests; assert structure not literal LLM text; `pytest` + `pytest-asyncio` only.**
- **Service URLs from env, never hardcoded.**
- **Default OFF / regression-safe:** `ENABLE_FINALIZE_REVISION` defaults to `"0"`. When off, the `revise` node returns `{}` (no state change) and the compiled graph behaves exactly as today.
- **Orthogonal to the critique gate:** do NOT touch `verify` / `verify_router` / `CRITIQUE_ARTIFACT_TYPES`. This is a separate, lighter, all-task-type pass.

---

## File Map

| File | Responsibility | Action |
|---|---|---|
| `services/orchestrator/finalize_revision.py` | Pure helpers: `should_revise(...) -> bool`, `build_revision_prompt(task, answer) -> str`. No I/O, no model calls. | **Create** |
| `services/orchestrator/graph.py` | Add `ENABLE_FINALIZE_REVISION` / `MAX_FINALIZE_REVISIONS` / `FINALIZE_REVISION_THINKING_BUDGET` env knobs; add a `_run_had_side_effects(state)` helper; add the `revise` node in `make_nodes`; re-wire `check → revise → END`. | **Modify** |
| `services/orchestrator/types.py` | Add additive `State` fields: `finalize_revisions: int`, `revised: bool`. No removals. | **Modify** |
| `tests/services/orchestrator/test_finalize_revision.py` | Exhaustive unit tests for the two pure helpers (side-effect guard, cap, empty/error skip, idempotency). | **Create** |
| `tests/services/orchestrator/test_graph_revise.py` | Unit tests for the `revise` node (mocked `orch.architect`) and the re-wired edge: revises once when enabled, pass-through when disabled, respects cap, skips on side-effects/empty/error. | **Create** |
| `tests/services/orchestrator/features/revise_before_deliver.feature` | `@mocked` Gherkin scenarios. | **Create** |
| `tests/services/orchestrator/test_revise_before_deliver_bdd.py` | pytest-bdd step defs binding the feature to the `revise` node via `fake_model` / patched `architect`. | **Create** |

---

## Behavior (BDD) — Gherkin

`tests/services/orchestrator/features/revise_before_deliver.feature`

```gherkin
@mocked
Feature: Revise the final answer before delivery
  As the orchestrator, before delivering a final answer I make at most one
  bounded model call to re-read the answer against the task and optionally
  replace it with a revised version — but only when it is safe and enabled.

  Background:
    Given the finalize-revision feature is enabled
    And the maximum finalize revisions is 1

  Scenario: An answer that needs revision is revised once before delivery
    Given a finalized state for task "List the prime numbers under 10"
    And the finalized answer is "2, 3, 5"
    And no side-effecting tools ran during the task
    And the run did not error
    And the revision model will return "2, 3, 5, 7"
    When the revise node runs
    Then the delivered final answer is "2, 3, 5, 7"
    And the revision model was called exactly 1 time
    And the finalize revision count is 1

  Scenario: An answer after a side-effecting tool run is NOT revised
    Given a finalized state for task "Create report.txt with the summary"
    And the finalized answer is "Created report.txt with the summary."
    And side-effecting tools ran during the task
    And the run did not error
    When the revise node runs
    Then the delivered final answer is "Created report.txt with the summary."
    And the revision model was called exactly 0 times

  Scenario: The revision cap is respected — at most MAX_FINALIZE_REVISIONS
    Given a finalized state for task "List the prime numbers under 10"
    And the finalized answer is "2, 3, 5"
    And no side-effecting tools ran during the task
    And the run did not error
    And the finalize revision count is already 1
    When the revise node runs
    Then the revision model was called exactly 0 times
    And the delivered final answer is "2, 3, 5"

  Scenario: The disabled flag delivers the answer unchanged
    Given the finalize-revision feature is disabled
    And a finalized state for task "List the prime numbers under 10"
    And the finalized answer is "2, 3, 5"
    And no side-effecting tools ran during the task
    And the run did not error
    When the revise node runs
    Then the delivered final answer is "2, 3, 5"
    And the revision model was called exactly 0 times

  Scenario: No visible answer means no revision
    Given a finalized state for task "Do something"
    And the finalized answer is ""
    And no side-effecting tools ran during the task
    And the run did not error
    When the revise node runs
    Then the revision model was called exactly 0 times

  Scenario: An errored run is not revised
    Given a finalized state for task "Fetch the data"
    And the finalized answer is "Failed subtasks: fetch (error: connection refused)"
    And no side-effecting tools ran during the task
    And the run errored with "1 subtask(s) failed"
    When the revise node runs
    Then the revision model was called exactly 0 times
    And the delivered final answer is "Failed subtasks: fetch (error: connection refused)"
```

---

## Task 1: Pure helpers — `should_revise` + `build_revision_prompt`

**Files:**
- Create: `services/orchestrator/finalize_revision.py`
- Test: `tests/services/orchestrator/test_finalize_revision.py`

**Interfaces:**
- Produces:
  - `should_revise(final_answer: str, *, had_side_effects: bool, attempts: int, max_attempts: int, errored: bool) -> bool`
  - `build_revision_prompt(task: str, answer: str) -> str`
- Consumes: nothing (pure; stdlib only).

- [ ] **Step 1: Write the failing tests for `should_revise`**

`tests/services/orchestrator/test_finalize_revision.py`:

```python
"""Unit tests for the pure revise-before-deliver helpers.

should_revise is the single side-effect / cap / empty / error guard; it makes
NO model call. build_revision_prompt formats the single revision prompt.
"""
import pytest

from services.orchestrator.finalize_revision import should_revise, build_revision_prompt


# ── should_revise: the happy path ────────────────────────────────────────────
def test_should_revise_true_when_clean_answer_under_cap():
    assert should_revise(
        "2, 3, 5",
        had_side_effects=False,
        attempts=0,
        max_attempts=1,
        errored=False,
    ) is True


# ── side-effect guard ────────────────────────────────────────────────────────
def test_should_revise_false_after_side_effects():
    assert should_revise(
        "Created report.txt.",
        had_side_effects=True,
        attempts=0,
        max_attempts=1,
        errored=False,
    ) is False


# ── cap guard (idempotency: a second pass on the same answer is blocked) ──────
def test_should_revise_false_at_cap():
    assert should_revise(
        "2, 3, 5",
        had_side_effects=False,
        attempts=1,
        max_attempts=1,
        errored=False,
    ) is False


def test_should_revise_false_over_cap():
    assert should_revise(
        "anything",
        had_side_effects=False,
        attempts=5,
        max_attempts=1,
        errored=False,
    ) is False


def test_should_revise_false_when_cap_is_zero():
    assert should_revise(
        "2, 3, 5",
        had_side_effects=False,
        attempts=0,
        max_attempts=0,
        errored=False,
    ) is False


# ── no visible text guard ────────────────────────────────────────────────────
@pytest.mark.parametrize("blank", ["", "   ", "\n\t  \n"])
def test_should_revise_false_when_no_visible_answer(blank):
    assert should_revise(
        blank,
        had_side_effects=False,
        attempts=0,
        max_attempts=1,
        errored=False,
    ) is False


# ── errored / aborted run guard ──────────────────────────────────────────────
def test_should_revise_false_when_errored():
    assert should_revise(
        "Failed subtasks: fetch (error: connection refused)",
        had_side_effects=False,
        attempts=0,
        max_attempts=1,
        errored=True,
    ) is False


# ── idempotency: same answer twice does not re-revise (attempts already spent) ─
def test_should_revise_idempotent_after_one_pass():
    # First pass allowed.
    assert should_revise("x", had_side_effects=False, attempts=0, max_attempts=1, errored=False) is True
    # After it ran once, attempts==1 -> blocked even with identical inputs otherwise.
    assert should_revise("x", had_side_effects=False, attempts=1, max_attempts=1, errored=False) is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/services/orchestrator/test_finalize_revision.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.orchestrator.finalize_revision'`.

- [ ] **Step 3: Write the prompt-builder failing test**

Append to `tests/services/orchestrator/test_finalize_revision.py`:

```python
# ── build_revision_prompt ────────────────────────────────────────────────────
def test_build_revision_prompt_includes_task_and_answer():
    p = build_revision_prompt("List primes under 10", "2, 3, 5")
    assert "List primes under 10" in p
    assert "2, 3, 5" in p


def test_build_revision_prompt_is_deterministic():
    a = build_revision_prompt("t", "ans")
    b = build_revision_prompt("t", "ans")
    assert a == b


def test_build_revision_prompt_keeps_answer_when_already_correct():
    # The instruction must permit returning the answer UNCHANGED (no forced edit).
    p = build_revision_prompt("t", "ans").lower()
    assert "unchanged" in p or "as is" in p or "return it unchanged" in p
```

- [ ] **Step 4: Run to verify the new tests fail**

Run: `python -m pytest tests/services/orchestrator/test_finalize_revision.py -q`
Expected: FAIL — module still missing.

- [ ] **Step 5: Implement the pure helpers**

`services/orchestrator/finalize_revision.py`:

```python
# services/orchestrator/finalize_revision.py
"""Pure helpers for the opt-in revise-before-deliver gate.

No I/O, no model calls, no graph imports — these are deterministic functions so
the side-effect guard, the revision cap, and the empty/error skips are
exhaustively unit-testable. The graph's `revise` node owns the single (bounded)
model call and reads these to decide whether to make it.
"""
from __future__ import annotations


def should_revise(
    final_answer: str,
    *,
    had_side_effects: bool,
    attempts: int,
    max_attempts: int,
    errored: bool,
) -> bool:
    """Return True iff it is safe and useful to make ONE more revision pass.

    Skip (return False) when:
      - there is no visible final answer (nothing to revise),
      - the run errored / aborted (don't paper over a failure),
      - side-effecting tools already ran (revising after actions is unsafe),
      - the revision cap is reached (attempts >= max_attempts) — this also makes
        the gate idempotent: once a pass has run, attempts==1 blocks a replay.
    """
    if errored:
        return False
    if had_side_effects:
        return False
    if final_answer is None or not final_answer.strip():
        return False
    if max_attempts <= 0:
        return False
    if attempts >= max_attempts:
        return False
    return True


def build_revision_prompt(task: str, answer: str) -> str:
    """Build the single revision prompt.

    Re-reads the draft answer against the original task. Crucially it permits
    returning the answer UNCHANGED so a correct answer is not gratuitously
    rewritten (no forced edit). Deterministic for a given (task, answer).
    """
    return (
        "You are reviewing a draft answer before it is delivered to the user.\n"
        "Re-read it against the original request. Check for: incompleteness "
        "(did it fully address the request?), unsupported claims, and fabricated "
        "or made-up facts.\n"
        "If the draft is already correct and complete, return it UNCHANGED. "
        "Otherwise return a corrected version. Reply with ONLY the final answer "
        "text — no preamble, no explanation of what you changed.\n\n"
        f"Original request:\n{task}\n\n"
        f"Draft answer:\n{answer}"
    )
```

- [ ] **Step 6: Run to verify all helper tests pass**

Run: `python -m pytest tests/services/orchestrator/test_finalize_revision.py -q`
Expected: PASS (all tests green).

- [ ] **Step 7: Commit**

```bash
git add services/orchestrator/finalize_revision.py tests/services/orchestrator/test_finalize_revision.py
git commit -m "feat(orchestrator): pure helpers for revise-before-deliver gate

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Additive `State` fields

**Files:**
- Modify: `services/orchestrator/types.py` (the `State` TypedDict, after the `skip_verify` field near line 76)
- Test: `tests/services/orchestrator/test_finalize_revision.py` (append a tiny presence test — no new file)

**Interfaces:**
- Produces: `State` keys `finalize_revisions: int` (count of revision passes taken) and `revised: bool` (True once the revise node replaced the answer). Both are default-absent (`State` is `total=False`).
- Consumes: nothing yet.

- [ ] **Step 1: Write the failing test for the new keys**

Append to `tests/services/orchestrator/test_finalize_revision.py`:

```python
# ── State fields exist (additive, total=False) ───────────────────────────────
def test_state_accepts_finalize_revision_fields():
    from services.orchestrator.types import State

    s: State = {"finalize_revisions": 1, "revised": True}
    assert s["finalize_revisions"] == 1
    assert s["revised"] is True
    # The annotations must declare them so static tools and readers see them.
    assert "finalize_revisions" in State.__annotations__
    assert "revised" in State.__annotations__
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/services/orchestrator/test_finalize_revision.py::test_state_accepts_finalize_revision_fields -q`
Expected: FAIL — `AssertionError` on `"finalize_revisions" in State.__annotations__`.

- [ ] **Step 3: Add the fields to `State`**

In `services/orchestrator/types.py`, immediately after the `skip_verify: bool` line (the conditional-gates block), add:

```python
    # Revise-before-deliver (opt-in; ENABLE_FINALIZE_REVISION). Additive, default-absent.
    finalize_revisions: int           # count of revise->revise passes taken (bounds the gate)
    revised: bool                     # True once the revise node replaced final_answer
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/services/orchestrator/test_finalize_revision.py::test_state_accepts_finalize_revision_fields -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/types.py tests/services/orchestrator/test_finalize_revision.py
git commit -m "feat(orchestrator): add finalize_revisions/revised State fields

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: `revise` node + env knobs + side-effect deriver in `graph.py`

**Files:**
- Modify: `services/orchestrator/graph.py`
  - env knobs near the other env reads (after `RATE_LIMIT_BACKOFF_SECONDS`, ~line 94)
  - `_run_had_side_effects(state)` module-level helper (after `collect_prior_reflections`, ~line 142)
  - `revise` node inside `make_nodes` (after the `verify` node, before `return ...`, ~line 691) and add it to the returned tuple
- Test: `tests/services/orchestrator/test_graph_revise.py`

**Interfaces:**
- Consumes: `should_revise`, `build_revision_prompt` (Task 1); `State` fields `finalize_revisions` (Task 2); `orch.architect(prompt, thinking_budget=...)`.
- Produces:
  - module helper `_run_had_side_effects(state: State) -> bool`
  - `make_nodes(...)` now returns an **8-tuple** ending with `revise_node` (was a 7-tuple). **Task 4 relies on this ordering.**
  - env knobs: `ENABLE_FINALIZE_REVISION` (default `"0"` → OFF), `MAX_FINALIZE_REVISIONS` (default `1`), `FINALIZE_REVISION_THINKING_BUDGET` (default `1024`).

- [ ] **Step 1: Write the failing tests for the `revise` node**

`tests/services/orchestrator/test_graph_revise.py`:

```python
"""Unit tests for the opt-in `revise` graph node and the check->revise->END wiring.

The revise node is the only place that makes the (bounded) revision model call.
orch.architect is mocked — no real inference.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

import services.orchestrator.graph as graph_mod
from services.orchestrator.graph import make_nodes, _run_had_side_effects


@pytest.fixture(autouse=True)
def _enable_feature(monkeypatch):
    """Default the feature ON for these tests by re-reading the env knobs.

    graph.py reads ENABLE_FINALIZE_REVISION at import time into a module global,
    so set the env AND patch the already-bound module globals.
    """
    monkeypatch.setenv("ENABLE_FINALIZE_REVISION", "1")
    monkeypatch.setattr(graph_mod, "ENABLE_FINALIZE_REVISION", True, raising=False)
    monkeypatch.setattr(graph_mod, "MAX_FINALIZE_REVISIONS", 1, raising=False)


def _nodes(architect):
    orch = MagicMock()
    orch.architect = architect
    async_orch = MagicMock()
    # make_nodes returns an 8-tuple ending with the revise node (Task 3).
    return make_nodes(orch, async_orch)


def _finalized_state(answer="2, 3, 5", task="List primes under 10", **extra):
    state = {
        "root_goal": task,
        "final_answer": answer,
        "last_artifact": {"type": "other", "payload": answer},
        "finalize_revisions": 0,
    }
    state.update(extra)
    return state


@pytest.mark.asyncio
async def test_revise_node_revises_once_when_enabled(monkeypatch):
    monkeypatch.setattr(graph_mod.events, "emit", AsyncMock())
    architect = AsyncMock(return_value="2, 3, 5, 7")
    nodes = _nodes(architect)
    revise = nodes[7]

    out = await revise(_finalized_state())

    architect.assert_awaited_once()
    assert out["final_answer"] == "2, 3, 5, 7"
    assert out["finalize_revisions"] == 1
    assert out["revised"] is True


@pytest.mark.asyncio
async def test_revise_node_passthrough_when_disabled(monkeypatch):
    monkeypatch.setattr(graph_mod, "ENABLE_FINALIZE_REVISION", False, raising=False)
    monkeypatch.setattr(graph_mod.events, "emit", AsyncMock())
    architect = AsyncMock(return_value="SHOULD NOT BE CALLED")
    nodes = _nodes(architect)
    revise = nodes[7]

    out = await revise(_finalized_state())

    architect.assert_not_awaited()
    assert out == {}  # no state change -> identical delivery to today


@pytest.mark.asyncio
async def test_revise_node_respects_cap(monkeypatch):
    monkeypatch.setattr(graph_mod.events, "emit", AsyncMock())
    architect = AsyncMock(return_value="2, 3, 5, 7")
    nodes = _nodes(architect)
    revise = nodes[7]

    out = await revise(_finalized_state(finalize_revisions=1))

    architect.assert_not_awaited()
    assert out == {}


@pytest.mark.asyncio
async def test_revise_node_skips_after_side_effects(monkeypatch):
    monkeypatch.setattr(graph_mod.events, "emit", AsyncMock())
    architect = AsyncMock(return_value="rewritten")
    nodes = _nodes(architect)
    revise = nodes[7]

    state = _finalized_state(
        answer="Created report.txt.",
        last_artifact={"type": "code", "payload": "wrote file"},
    )
    out = await revise(state)

    architect.assert_not_awaited()
    assert out == {}


@pytest.mark.asyncio
async def test_revise_node_skips_when_errored(monkeypatch):
    monkeypatch.setattr(graph_mod.events, "emit", AsyncMock())
    architect = AsyncMock(return_value="rewritten")
    nodes = _nodes(architect)
    revise = nodes[7]

    out = await revise(_finalized_state(error="1 subtask(s) failed"))

    architect.assert_not_awaited()
    assert out == {}


@pytest.mark.asyncio
async def test_revise_node_skips_when_no_visible_answer(monkeypatch):
    monkeypatch.setattr(graph_mod.events, "emit", AsyncMock())
    architect = AsyncMock(return_value="rewritten")
    nodes = _nodes(architect)
    revise = nodes[7]

    out = await revise(_finalized_state(answer="   "))

    architect.assert_not_awaited()
    assert out == {}
```

- [ ] **Step 2: Write the failing tests for `_run_had_side_effects`**

Append to `tests/services/orchestrator/test_graph_revise.py`:

```python
# ── _run_had_side_effects: conservative side-effect signal ───────────────────
def test_had_side_effects_true_for_code_artifact():
    assert _run_had_side_effects({"last_artifact": {"type": "code", "payload": "x"}}) is True


def test_had_side_effects_false_for_plain_text_artifact():
    assert _run_had_side_effects({"last_artifact": {"type": "other", "payload": "x"}}) is False


def test_had_side_effects_false_for_writing_artifact():
    # 'writing' is a long prose answer, not a side effect.
    assert _run_had_side_effects({"last_artifact": {"type": "writing", "payload": "x"}}) is False


def test_had_side_effects_false_when_absent():
    assert _run_had_side_effects({}) is False
```

- [ ] **Step 3: Run to verify the node + helper tests fail**

Run: `python -m pytest tests/services/orchestrator/test_graph_revise.py -q`
Expected: FAIL — `ImportError: cannot import name '_run_had_side_effects'` and `make_nodes` returns a 7-tuple (`IndexError` on `nodes[7]`).

- [ ] **Step 4: Add the env knobs**

In `services/orchestrator/graph.py`, after the `RATE_LIMIT_BACKOFF_SECONDS` line (~line 94), add:

```python
# Revise-before-deliver (opt-in). A lightweight single-pass gate that re-reads the
# final answer against the task and may produce ONE revised answer before delivery.
# OFF by default — like the critique auto-gate — so it never adds latency to every
# answer. ORTHOGONAL to the heavy critique verify-gate (verify node); this is the
# general-purpose lightweight pass for all task types.
ENABLE_FINALIZE_REVISION = os.getenv("ENABLE_FINALIZE_REVISION", "0") not in (
    "0",
    "false",
    "False",
    "",
)
MAX_FINALIZE_REVISIONS = int(os.getenv("MAX_FINALIZE_REVISIONS", "1"))
FINALIZE_REVISION_THINKING_BUDGET = int(
    os.getenv("FINALIZE_REVISION_THINKING_BUDGET", "1024")
)

from .finalize_revision import should_revise, build_revision_prompt
```

- [ ] **Step 5: Add the `_run_had_side_effects` module helper**

In `services/orchestrator/graph.py`, after `collect_prior_reflections` (~line 142), add:

```python
def _run_had_side_effects(state: "State") -> bool:
    """Conservative heuristic: did this run execute a side-effecting tool/skill?

    Revising after side effects is unsafe (the answer describes actions already
    taken), so the revise gate must skip in that case. We approximate "had side
    effects" from the LAST artifact's type: 'code' artifacts come from skills that
    write/edit files or run code in the sandbox (a side effect); 'writing' and
    'other' are read-only prose answers. This is intentionally conservative — when
    unsure, prefer NOT revising. Pure: reads only state['last_artifact'].
    """
    artifact = state.get("last_artifact") or {}
    return artifact.get("type") == "code"
```

- [ ] **Step 6: Add the `revise` node and return it from `make_nodes`**

In `services/orchestrator/graph.py`, inside `make_nodes`, immediately after the `verify` node's `return result` and before `return plan, execute_node, ...` (~line 691), add the node:

```python
    async def revise(state: State) -> dict:
        """Opt-in lightweight revise-before-deliver gate.

        After `check` finalized the answer (final_answer set) and BEFORE delivery,
        make at most MAX_FINALIZE_REVISIONS bounded model calls to re-read the
        answer against the task and optionally replace it. Pass-through (returns {})
        when disabled or when should_revise() says it is unsafe/unnecessary, so the
        delivered answer is identical to today.
        """
        import copy

        if not ENABLE_FINALIZE_REVISION:
            return {}

        final_answer = state.get("final_answer") or ""
        errored = state.get("error") is not None
        attempts = int(state.get("finalize_revisions", 0))
        had_side_effects = _run_had_side_effects(state)

        if not should_revise(
            final_answer,
            had_side_effects=had_side_effects,
            attempts=attempts,
            max_attempts=MAX_FINALIZE_REVISIONS,
            errored=errored,
        ):
            return {}

        task = state.get("root_goal") or final_answer
        prompt = build_revision_prompt(task, final_answer)
        revised = await orch.architect(
            prompt, thinking_budget=FINALIZE_REVISION_THINKING_BUDGET
        )
        revised = (revised or "").strip()
        # A blank revision is a model glitch — keep the original answer but still
        # spend the attempt so the gate stays bounded/idempotent.
        out_answer = revised if revised else final_answer

        await events.emit(
            "reasoning",
            node="revise",
            summary="revised final answer" if revised and revised != final_answer
            else "kept final answer unchanged",
            text=out_answer[:500],
        )

        # Mirror final_answer onto the root goal's result so every delivery path
        # (stream_final_answer reads final_answer; clarification/direct paths read
        # final_answer) sees the revised text.
        tree = copy.deepcopy(state["goal_tree"]) if state.get("goal_tree") else None
        if tree is not None and "root" in tree:
            update_status(tree, "root", Status(tree["root"]["status"]), result=out_answer)

        result: dict = {
            "final_answer": out_answer,
            "finalize_revisions": attempts + 1,
            "revised": bool(revised and revised != final_answer),
        }
        if tree is not None:
            result["goal_tree"] = tree
        return result
```

Then change the return line of `make_nodes` from:

```python
    return plan, execute_node, check, reflect, approval, assess_ambiguity, verify
```

to:

```python
    return plan, execute_node, check, reflect, approval, assess_ambiguity, verify, revise
```

- [ ] **Step 7: Run the node + helper tests to verify they pass**

Run: `python -m pytest tests/services/orchestrator/test_graph_revise.py -q`
Expected: PASS (all green).

- [ ] **Step 8: Run the full existing graph suite to confirm no regression from the 8-tuple change yet**

Run: `python -m pytest tests/services/orchestrator/test_graph.py -q`
Expected: PASS — `make_nodes` callers in existing tests that unpack `plan, execute_node, check, reflect, approval, assess_ambiguity, verify` will now have a trailing item. **If any existing test unpacks all 7 names with `a, b, c, d, e, f, g = make_nodes(...)`, that line raises `ValueError: too many values to unpack`.** Fix each such call site to either index (`make_nodes(...)[N]`) or add a trailing `, _revise`. (The repo idiom is `nodes = make_nodes(...); x = nodes[i]`, which is unaffected.)

If Step 8 reveals a tuple-unpack call site, fix it minimally (append `, _revise` to the unpack) and re-run until green before committing.

- [ ] **Step 9: Commit**

```bash
git add services/orchestrator/graph.py tests/services/orchestrator/test_graph_revise.py
git commit -m "feat(orchestrator): add opt-in revise-before-deliver node

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Wire `check -> revise -> END` into the compiled graph

**Files:**
- Modify: `services/orchestrator/graph.py` (`build_graph` — node registration ~line 788-799, edges ~line 801-808, and `router`'s `END` return)
- Test: `tests/services/orchestrator/test_graph_revise.py` (append integration-style edge tests)

**Interfaces:**
- Consumes: the 8-tuple from `make_nodes` (Task 3).
- Produces: a graph where `check` finalization flows `check -> revise -> END` when enabled, and `check -> revise (pass-through) -> END` when disabled (identical delivery). `router`'s other branches (`execute`/`reflect`/`approval`) are unchanged.

- [ ] **Step 1: Write the failing wiring tests**

Append to `tests/services/orchestrator/test_graph_revise.py`:

```python
# ── router still ENDs on non-final states; revise is only reached post-check ──
def test_router_unchanged_for_non_final_states():
    from services.orchestrator.graph import router
    from langgraph.graph import END

    # No final_answer, no goal -> END (unchanged behavior).
    assert router({"current_goal_id": None}) == END


def test_build_graph_registers_revise_node(monkeypatch):
    """build_graph must add a 'revise' node and route check -> revise.

    We avoid a live MongoDB by asserting on the StateGraph builder before compile
    is exercised; here we just confirm make_nodes yields 8 nodes and the revise
    node is callable, which build_graph consumes.
    """
    from services.orchestrator.graph import make_nodes

    nodes = make_nodes(MagicMock(), MagicMock())
    assert len(nodes) == 8
    assert callable(nodes[7])
```

- [ ] **Step 2: Run to verify the wiring tests fail or pass appropriately**

Run: `python -m pytest tests/services/orchestrator/test_graph_revise.py::test_build_graph_registers_revise_node tests/services/orchestrator/test_graph_revise.py::test_router_unchanged_for_non_final_states -q`
Expected: `test_router_unchanged_for_non_final_states` PASSES (router unchanged); `test_build_graph_registers_revise_node` PASSES after Task 3 (8 nodes). If `len(nodes) == 8` fails, Task 3 was not applied — stop and fix.

- [ ] **Step 3: Register the `revise` node in `build_graph`**

In `services/orchestrator/graph.py` `build_graph`, change the `make_nodes` unpack from:

```python
    plan_node, execute_node, check_node, reflect_node, approval_node, assess_node, verify_node = make_nodes(
        orch, async_orch
    )
```

to:

```python
    plan_node, execute_node, check_node, reflect_node, approval_node, assess_node, verify_node, revise_node = make_nodes(
        orch, async_orch
    )
```

and add the node registration after `b.add_node("assess_ambiguity", assess_node)`:

```python
    b.add_node("revise", revise_node)
```

- [ ] **Step 4: Re-point `check`'s terminal edge through `revise`**

`router` returns `END` when `final_answer` is set. To route that terminal case through `revise` instead, change the `check` conditional edge target list and add the `revise -> END` edge. Replace:

```python
    b.add_conditional_edges("check", router, ["execute", "reflect", "approval", END])
```

with:

```python
    b.add_conditional_edges("check", router, ["execute", "reflect", "approval", "revise", END])
    b.add_edge("revise", END)
```

and update `router` so the finalized branch returns `"revise"` instead of `END`. In `router`, change:

```python
    # If final_answer is set, check node finalized the tree — end execution
    if state.get("final_answer"):
        return END
```

to:

```python
    # If final_answer is set, check node finalized the tree — run the opt-in
    # revise-before-deliver gate before ending. The revise node is a pass-through
    # (returns {}) when the feature is disabled, so delivery is identical to today.
    if state.get("final_answer"):
        return "revise"
```

> **Important:** `router` is also reached from `check` after `revise` only via the `revise -> END` edge, NOT back into `router`. `revise` does NOT set any new `current_goal_id`, and its `final_answer` is already set, so there is no risk of a `check -> revise -> check` loop. `revise` always terminates at `END`.

- [ ] **Step 5: Update the disabled-path test to assert delivery is unchanged**

Append to `tests/services/orchestrator/test_graph_revise.py`:

```python
@pytest.mark.asyncio
async def test_router_routes_finalized_to_revise(monkeypatch):
    """When final_answer is set, router returns 'revise' (the gate), not END."""
    from services.orchestrator.graph import router

    assert router({"final_answer": "done", "current_goal_id": "root",
                   "goal_tree": {"root": {"status": "completed", "attempts": 0}}}) == "revise"
```

- [ ] **Step 6: Run the focused wiring tests**

Run: `python -m pytest tests/services/orchestrator/test_graph_revise.py -q`
Expected: PASS (all green, including `test_router_routes_finalized_to_revise`).

- [ ] **Step 7: Run the full orchestrator suite to confirm no regression**

Run: `python -m pytest tests/services/orchestrator/ -q`
Expected: PASS. The `router` change means every finalized run now passes through `revise`; with the feature OFF by default the node returns `{}` so existing end-to-end graph tests still observe the same `final_answer`. If a test asserted the literal route string `END` directly from `router` on a finalized state, update it to expect `"revise"` (only `test_graph.py` router tests, if any — search for `router(` returning `END` on a `final_answer` state).

- [ ] **Step 8: Commit**

```bash
git add services/orchestrator/graph.py tests/services/orchestrator/test_graph_revise.py
git commit -m "feat(orchestrator): wire check->revise->END for revise-before-deliver

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: BDD scenarios — `revise_before_deliver.feature` + step defs

**Files:**
- Create: `tests/services/orchestrator/features/revise_before_deliver.feature` (content in the "Behavior (BDD)" section above)
- Create: `tests/services/orchestrator/test_revise_before_deliver_bdd.py`
- Test: the BDD file itself.

**Interfaces:**
- Consumes: the `revise` node (`make_nodes(...)[7]`), `graph_mod` module globals (`ENABLE_FINALIZE_REVISION`, `MAX_FINALIZE_REVISIONS`), `run_async` helper from `tests/conftest.py`. The model call is mocked by patching `orch.architect` (the established idiom in `test_graph.py` / `test_ambiguity_clarification.py`) — this node calls `orch.architect`, so we patch that directly rather than the HTTP seam.

- [ ] **Step 1: Create the feature file**

Create `tests/services/orchestrator/features/revise_before_deliver.feature` with the full Gherkin from the "Behavior (BDD)" section above (copy it verbatim).

- [ ] **Step 2: Write the step-def file (will fail until bound)**

`tests/services/orchestrator/test_revise_before_deliver_bdd.py`:

```python
"""pytest-bdd step defs for revise-before-deliver.

@mocked: the revise node's single model call is the orchestrator's
orch.architect, which we replace with an AsyncMock (the established graph-test
idiom). No HTTP, no GPU.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from pytest_bdd import scenarios, given, when, then, parsers

import services.orchestrator.graph as graph_mod
from services.orchestrator.graph import make_nodes
from tests.conftest import run_async

scenarios("features/revise_before_deliver.feature")


@pytest.fixture
def ctx(monkeypatch):
    """Mutable scenario context; defaults the feature ON + emit patched."""
    monkeypatch.setattr(graph_mod, "ENABLE_FINALIZE_REVISION", True, raising=False)
    monkeypatch.setattr(graph_mod, "MAX_FINALIZE_REVISIONS", 1, raising=False)
    monkeypatch.setattr(graph_mod.events, "emit", AsyncMock())
    architect = AsyncMock(return_value="UNSET")
    return {
        "architect": architect,
        "state": {
            "last_artifact": {"type": "other", "payload": ""},
            "finalize_revisions": 0,
        },
        "out": None,
    }


# ── Background ────────────────────────────────────────────────────────────────
@given("the finalize-revision feature is enabled")
def _enabled(ctx, monkeypatch):
    monkeypatch.setattr(graph_mod, "ENABLE_FINALIZE_REVISION", True, raising=False)


@given("the finalize-revision feature is disabled")
def _disabled(ctx, monkeypatch):
    monkeypatch.setattr(graph_mod, "ENABLE_FINALIZE_REVISION", False, raising=False)


@given(parsers.parse("the maximum finalize revisions is {n:d}"))
def _max(ctx, monkeypatch, n):
    monkeypatch.setattr(graph_mod, "MAX_FINALIZE_REVISIONS", n, raising=False)


# ── State setup ───────────────────────────────────────────────────────────────
@given(parsers.parse('a finalized state for task "{task}"'))
def _task(ctx, task):
    ctx["state"]["root_goal"] = task
    ctx["state"]["goal_tree"] = {
        "root": {"status": "completed", "attempts": 0, "result": "", "children": []}
    }


@given(parsers.parse('the finalized answer is "{answer}"'))
def _answer(ctx, answer):
    ctx["state"]["final_answer"] = answer


@given(parsers.parse('the finalized answer is ""'))
def _answer_blank(ctx):
    ctx["state"]["final_answer"] = ""


@given("no side-effecting tools ran during the task")
def _no_side_effects(ctx):
    ctx["state"]["last_artifact"] = {"type": "other", "payload": ""}


@given("side-effecting tools ran during the task")
def _side_effects(ctx):
    ctx["state"]["last_artifact"] = {"type": "code", "payload": "wrote a file"}


@given("the run did not error")
def _no_error(ctx):
    ctx["state"].pop("error", None)


@given(parsers.parse('the run errored with "{msg}"'))
def _errored(ctx, msg):
    ctx["state"]["error"] = msg


@given(parsers.parse("the finalize revision count is already {n:d}"))
def _count_already(ctx, n):
    ctx["state"]["finalize_revisions"] = n


@given(parsers.parse('the revision model will return "{text}"'))
def _model_returns(ctx, text):
    ctx["architect"] = AsyncMock(return_value=text)


# ── Action ────────────────────────────────────────────────────────────────────
@when("the revise node runs")
def _run(ctx):
    orch = MagicMock()
    orch.architect = ctx["architect"]
    nodes = make_nodes(orch, MagicMock())
    revise = nodes[7]
    ctx["out"] = run_async(revise(ctx["state"]))


# ── Assertions ────────────────────────────────────────────────────────────────
@then(parsers.parse('the delivered final answer is "{expected}"'))
def _delivered(ctx, expected):
    out = ctx["out"] or {}
    # When the node passes through (out == {}), the delivered answer is the
    # original final_answer in state; otherwise it's the node's final_answer.
    delivered = out.get("final_answer", ctx["state"].get("final_answer"))
    assert delivered == expected


@then(parsers.parse("the revision model was called exactly {n:d} time"))
@then(parsers.parse("the revision model was called exactly {n:d} times"))
def _called(ctx, n):
    assert ctx["architect"].await_count == n


@then(parsers.parse("the finalize revision count is {n:d}"))
def _count(ctx, n):
    out = ctx["out"] or {}
    assert out.get("finalize_revisions", ctx["state"].get("finalize_revisions")) == n
```

- [ ] **Step 3: Run the BDD scenarios to verify they pass**

Run: `python -m pytest tests/services/orchestrator/test_revise_before_deliver_bdd.py -q`
Expected: PASS — all six scenarios green.

- [ ] **Step 4: Confirm the `bdd`/`@mocked` marker is recognized (no marker warnings)**

Run: `python -m pytest tests/services/orchestrator/test_revise_before_deliver_bdd.py -q -W error::pytest.PytestUnknownMarkWarning`
Expected: PASS (the `@mocked` tag + `bdd` marker are already registered in `pytest.ini` per the BDD foundation). If a marker warning surfaces, the scenario tag is fine — pytest-bdd tags do not require registration; only address it if pytest errors.

- [ ] **Step 5: Commit**

```bash
git add tests/services/orchestrator/features/revise_before_deliver.feature tests/services/orchestrator/test_revise_before_deliver_bdd.py
git commit -m "test(orchestrator): BDD scenarios for revise-before-deliver

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Full-suite regression + default-OFF proof

**Files:**
- Test only (no source changes unless a regression surfaces).

- [ ] **Step 1: Run the orchestrator + memory suites with the feature at its DEFAULT (off)**

Run: `python -m pytest tests/services/orchestrator/ tests/services/memory/ -q`
Expected: PASS. Confirms the default-OFF graph behaves identically to before this branch (the `revise` node returns `{}` for every finalized run). Per CLAUDE.md the prior baseline is **684 passed**; the new tests add to that count — expect `684 + N passed` where N is the count of new tests in Tasks 1–5.

- [ ] **Step 2: Prove the feature toggles ON cleanly with an explicit env run**

Run: `ENABLE_FINALIZE_REVISION=1 python -m pytest tests/services/orchestrator/test_graph_revise.py tests/services/orchestrator/test_revise_before_deliver_bdd.py -q`
Expected: PASS — the node-level tests already force the flag via monkeypatch, so this is a belt-and-suspenders check that env parsing works.

- [ ] **Step 3: Grep for accidental stdout writes in the new code**

Run: `grep -rn "print(" services/orchestrator/finalize_revision.py services/orchestrator/graph.py | grep -v "#"`
Expected: no matches (stdout-sacred honored).

- [ ] **Step 4: Final commit if any regression fix was needed**

```bash
git add -A
git commit -m "test(orchestrator): full-suite regression for revise-before-deliver

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

(If Steps 1–3 were already green with nothing to change, skip this commit.)

---

## Self-Review

**1. Spec coverage** — each requirement maps to a task:

| Spec requirement | Task |
|---|---|
| Opt-in single-pass revise gate before delivery | Task 3 (node) + Task 4 (wiring) |
| New module `finalize_revision.py` with `should_revise` + `build_revision_prompt` | Task 1 |
| `should_revise` signature `(final_answer, *, had_side_effects, attempts, max_attempts, errored) -> bool` | Task 1 |
| `build_revision_prompt(task, answer) -> str` | Task 1 |
| Cleanest seam decided + justified, `check` stays deterministic | "Chosen Seam" section + Task 3 (node, not folded into `check`) |
| Side-effect guard (no revise after side-effecting tools) | Task 1 (`should_revise` arg) + Task 3 (`_run_had_side_effects`) + BDD scenario 2 |
| Cap respected (`MAX_FINALIZE_REVISIONS`, default 1) | Task 1 cap tests + Task 3 + BDD scenario 3 |
| Idempotency (same answer twice doesn't re-revise) | Task 1 `test_should_revise_idempotent_after_one_pass` + cap behavior |
| No-visible-text skip | Task 1 parametrized blank test + Task 3 + BDD scenario 5 |
| Errored/aborted skip | Task 1 `test_should_revise_false_when_errored` + Task 3 + BDD scenario 6 |
| Env `ENABLE_FINALIZE_REVISION` default OFF | Task 3 env knob + `test_revise_node_passthrough_when_disabled` + BDD scenario 4 |
| Env `MAX_FINALIZE_REVISIONS` default 1 | Task 3 env knob |
| Model call mocked in tests | Task 3 (`AsyncMock` architect), Task 5 (`AsyncMock` architect) |
| Default OFF → graph + delivery identical | Task 3 passthrough test + Task 6 Step 1 |
| `thinking_budget_tokens` set / `api_key="not-needed"` | Task 3 routes through `orch.architect` (already compliant) |
| stdout-sacred / asyncio-correct | Task 6 Step 3 grep + all nodes `async def` |
| Full `.feature` Gherkin | "Behavior (BDD)" section + Task 5 |
| Does NOT touch critique gate | No task modifies `verify`/`verify_router`/`CRITIQUE_ARTIFACT_TYPES` |

**2. Placeholder scan** — searched for "TBD/TODO/handle edge cases/add validation/similar to Task N": none present. Every code step shows complete code. The "fix each call site" instruction in Task 3 Step 8 is a concrete conditional with the exact failure mode and fix shown.

**3. Type consistency** —
- `should_revise(final_answer, *, had_side_effects, attempts, max_attempts, errored)` — identical signature in Task 1 definition, Task 1 tests, and Task 3 call site.
- `build_revision_prompt(task, answer)` — identical in Task 1 def, Task 1 tests, Task 3 call.
- `make_nodes` returns an **8-tuple** with `revise` last — established in Task 3 (return line), consumed by Task 3 tests (`nodes[7]`), Task 4 (`build_graph` unpack adds `revise_node`), and Task 5 (`nodes[7]`). Consistent throughout.
- `_run_had_side_effects(state) -> bool` — defined Task 3, tested Task 3, called in the `revise` node. Consistent.
- State fields `finalize_revisions: int`, `revised: bool` — defined Task 2, written by the `revise` node (Task 3), read in Task 3/Task 5 tests. Consistent.
- Env globals `ENABLE_FINALIZE_REVISION` (bool), `MAX_FINALIZE_REVISIONS` (int), `FINALIZE_REVISION_THINKING_BUDGET` (int) — defined Task 3, patched in tests Task 3/Task 5. Consistent.

One risk noted inline (Task 3 Step 8 / Task 4 Step 7): existing tests that hard-unpack the 7-tuple or assert `router(...) == END` on a finalized state must be updated to the 8-tuple / `"revise"` return. Both are flagged with the exact symptom and fix, and Task 6 re-runs the full suite to catch any miss.
