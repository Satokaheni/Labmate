# Reconcile the Final Rendered Answer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A goal whose skill returned `ok=True` but whose **user-facing rendered final answer** is a punt ("I couldn't analyze the file because it is too large… please share a snippet") must end `ok=False` in the Redis result payload.

**Architecture:** Add one PURE, side-effect-free wrapper (`reconcile_final_answer`) to the existing `services/orchestrator/completion_guard.py` that reuses `is_punt_answer`/`reconcile_ok` (no new phrase list). Wire it at the single seam in `services/orchestrator/main.py::OrchestratorProcess._handle` where BOTH the rendered final answer AND the `error`/`ok` flag are available before the result leaves the orchestrator: after `stream_final_answer` writes the rendered answer into `final_state["final_answer"]`, and *before* `ok_flag = final_state.get("error") is None`. On a punt, set `final_state["error"]` so the correction propagates to (a) the immediate `ok_flag`, (b) the Redis `labmate:result:<task_id>` payload, (c) the `finally`-block `ok_flag`/`turn.done` status, and (d) `complete_session`.

**Tech Stack:** Python 3.11, asyncio, pytest, pytest-asyncio, pytest-bdd, `unittest.mock`, `respx` (HTTP seam, not needed here), the existing `fake_model` / `run_async` test helpers in `tests/conftest.py`.

## Global Constraints

These apply to EVERY task below (copied from `CLAUDE.md`):

- **stdout is sacred.** Never `print()` / `console.log()` in orchestrator code. Diagnostics go to `logging` (stderr) via `logging.getLogger("orchestrator")`.
- **Every model call sets `thinking_budget_tokens`** via `extra_body=` AND `api_key="not-needed"`. (This plan adds NO new model call — pure logic only — but any edit near a call must preserve these.)
- **Reuse `completion_guard`** — do NOT introduce a second punt-phrase list or duplicate the heuristics. The new helper composes the existing `is_punt_answer` / `reconcile_ok`.
- **Additive + regression-safe.** No removals. The two existing reconcile seams (`coding_orchestrator.py` `_run_skill_first` and the `_run_react_loop` finish) stay untouched and keep passing. A genuine success with a normal final answer stays `ok=True`; a verified fix stays `ok=True`.
- **Pure logic stays pure** — the new `reconcile_final_answer` does no I/O, no logging, no mutation of its inputs; it returns a value the caller acts on. It is unit-tested without a model.
- **File naming:** Python files `snake_case.py`, functions `snake_case`, classes `PascalCase`.
- **Tests** live under `tests/services/orchestrator/` mirroring `services/`; async tests use `@pytest.mark.asyncio`; BDD features live in `tests/services/orchestrator/features/<slug>.feature` tagged `@mocked`, with step defs in `tests/services/orchestrator/test_<slug>_bdd.py`. Assert structure, not literal LLM text.

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `services/orchestrator/completion_guard.py` | PURE reconciliation heuristics. Add `reconcile_final_answer(ok, error, answer, *, tests_passed=False)` composing the existing functions. | Modify |
| `services/orchestrator/main.py` | `_handle` goal loop. Wire the new helper at the post-render / pre-`ok_flag` seam so a punt sets `final_state["error"]`. | Modify |
| `tests/services/orchestrator/test_completion_guard.py` | Pure unit tests for `reconcile_final_answer`. | Modify (append) |
| `tests/services/orchestrator/features/final_answer_reconcile.feature` | Gherkin behavior spec, `@mocked`. | Create |
| `tests/services/orchestrator/test_final_answer_reconcile_bdd.py` | pytest-bdd step defs driving `_handle` end-to-end through the Redis result payload. | Create |
| `tests/services/orchestrator/test_main_final_answer_reconcile.py` | Async integration tests on `_handle` asserting the stored payload `ok`. | Create |

**Chosen seam justification.** The punt wording is produced *downstream* of the existing reconcile sites: by the summarizer (`coding_orchestrator.py` ~`_run_skill_first`/replan synth ~line 1361) and/or `stream_final_answer` (~line 1652). Neither re-reconciles, and the graph `check` node (`graph.py` ~line 350, sets `final_answer` ~line 466) runs *before* `stream_final_answer` overwrites `final_answer` with the rendered text — so reconciling inside `check` would inspect the pre-render assembled answer, not the punt the user actually sees. The ONLY point where the **rendered** answer AND the ok/status both exist, before the result is written to Redis, is in `main.py::_handle` immediately after `stream_final_answer` writes back `final_state["final_answer"]` and immediately before `ok_flag` is derived. Setting `final_state["error"]` there is the correct propagation lever because `ok_flag` (immediate write, ~line 579) AND the `finally` recomputation (~lines 636-637, drives `complete_session` and `turn.done` status) BOTH read `final_state.get("error")`. This ADDS a third reconciliation; it does not touch or regress the two existing ones.

---

### Task 1: Pure `reconcile_final_answer` helper in completion_guard

**Files:**
- Modify: `services/orchestrator/completion_guard.py` (append a new function after `reconcile_ok`)
- Test: `tests/services/orchestrator/test_completion_guard.py` (append)

**Interfaces:**
- Consumes: existing `is_punt_answer(text) -> bool` and `reconcile_ok(ok, answer, *, tests_passed) -> tuple[bool, str]` from the same module (already present, verified).
- Produces: `reconcile_final_answer(ok: bool, error: str | None, answer: str, *, tests_passed: bool = False) -> tuple[bool, str | None, str]` returning `(corrected_ok, corrected_error, note)`. Pure; no logging, no mutation of inputs. `corrected_error` is the value the caller assigns to `final_state["error"]` (preserving an already-set error, adding one only on a fresh downgrade). `note` is the empty string when nothing changed.

- [ ] **Step 1: Write the failing tests**

Append to `tests/services/orchestrator/test_completion_guard.py`:

```python
# ── reconcile_final_answer (rendered-answer seam) ───────────────────────────────

from services.orchestrator.completion_guard import reconcile_final_answer


def test_final_punt_with_ok_true_and_no_error_downgrades_and_sets_error():
    # The open A/B bug: skill returned ok=True (no error), but the RENDERED
    # final answer is a punt -> must become ok=False with an error set.
    ok, error, note = reconcile_final_answer(
        True,
        None,
        "I couldn't analyze the file because it is too large. Please share a snippet.",
    )
    assert ok is False
    assert error  # a non-empty error string is now set
    assert "punt" in error.lower() or "could not" in error.lower()
    assert note


def test_final_genuine_success_answer_unchanged():
    # Regression: a normal, non-punt success answer stays ok=True, no error added.
    ok, error, note = reconcile_final_answer(
        True,
        None,
        "Here is the square function you asked for.",
    )
    assert ok is True
    assert error is None
    assert note == ""


def test_final_verified_fix_stays_ok():
    # Regression: a verified fix ("tests pass") with tests_passed=True stays ok.
    ok, error, note = reconcile_final_answer(
        True,
        None,
        "I fixed the off-by-one bug and all tests pass.",
        tests_passed=True,
    )
    assert ok is True
    assert error is None
    assert note == ""


def test_final_unverified_success_claim_downgrades():
    # An unverified "I fixed it / tests pass" claim (no passing run) -> ok=False.
    ok, error, note = reconcile_final_answer(
        True,
        None,
        "I fixed the off-by-one bug and all tests pass.",
        tests_passed=False,
    )
    assert ok is False
    assert error
    assert note


def test_final_preexisting_error_preserved_not_overwritten():
    # If error was already set upstream, keep it (do not clobber the real cause).
    ok, error, note = reconcile_final_answer(
        False,
        "2 subtask(s) failed: parse (error: boom)",
        "I could not process the file, it is too large.",
    )
    assert ok is False
    assert error == "2 subtask(s) failed: parse (error: boom)"
    assert note == ""  # already-failed input -> no extra downgrade note


def test_final_empty_answer_is_noop():
    # No rendered answer -> nothing to reconcile; pass through unchanged.
    ok, error, note = reconcile_final_answer(True, None, "")
    assert ok is True
    assert error is None
    assert note == ""


def test_final_inputs_not_mutated():
    # Purity: the function returns new values; passing None error stays None
    # on a clean answer.
    answer = "All good, here is the result."
    ok, error, note = reconcile_final_answer(True, None, answer)
    assert (ok, error, note) == (True, None, "")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_completion_guard.py -k final -v`
Expected: FAIL — `ImportError: cannot import name 'reconcile_final_answer'` (collection error on the new import).

- [ ] **Step 3: Write the minimal implementation**

Append to `services/orchestrator/completion_guard.py`, after `reconcile_ok` (keep it pure — reuse `reconcile_ok`, no new phrase list):

```python
def reconcile_final_answer(
    ok: bool,
    error: str | None,
    answer: str,
    *,
    tests_passed: bool = False,
) -> tuple[bool, str | None, str]:
    """Reconcile the *rendered* final answer (post-summarizer) with ``ok``/``error``.

    This is the THIRD reconciliation seam (after the skill-first raw-output and the
    ReAct finish seams). The punt wording is produced downstream by the final-answer
    summarizer / stream_final_answer, so the user-facing answer must be re-checked
    before the result leaves the orchestrator.

    Reuses ``reconcile_ok`` (no new phrase list). Returns
    ``(corrected_ok, corrected_error, note)``:

      * If ``reconcile_ok`` downgrades ``ok`` (a punt, or an unverified success
        claim) -> ``corrected_ok=False``. An ``error`` is set so the downgrade
        propagates through the result payload's ``ok`` derivation. A pre-existing
        ``error`` is PRESERVED (never clobbered — keep the real upstream cause);
        only a fresh downgrade (no prior error) gets the honesty note as its error.
      * Otherwise everything is returned unchanged.

    Pure: never mutates its inputs, never logs, never does I/O.
    """
    new_ok, note = reconcile_ok(ok, answer, tests_passed=tests_passed)
    if new_ok == ok:
        # No change (genuine success, empty answer, or already-False input).
        return ok, error, note
    # A downgrade happened. Set an error if none exists; otherwise keep the
    # original (more specific) error.
    new_error = error if error else (note or "final answer reconciled to not-success")
    return new_ok, new_error, note
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_completion_guard.py -v`
Expected: PASS (all prior completion_guard tests + the 7 new ones).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/completion_guard.py tests/services/orchestrator/test_completion_guard.py
git commit -m "feat(completion-guard): add pure reconcile_final_answer for rendered-answer seam

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Wire the rendered-answer reconciliation into _handle

**Files:**
- Modify: `services/orchestrator/main.py` (`OrchestratorProcess._handle`, between the `stream_final_answer` write-back and the `ok_flag` derivation)
- Test: `tests/services/orchestrator/test_main_final_answer_reconcile.py` (create)

**Interfaces:**
- Consumes: `reconcile_final_answer` from Task 1.
- Produces: corrected `final_state["error"]` (and therefore the stored payload `ok`) whenever the rendered `final_answer` is a punt or an unverified success claim. No new public symbol.

> **ANCHOR ON STRUCTURE, not line numbers.** Live work may shift lines. Locate the seam by these landmarks in `_handle`: the `elif hasattr(orch, "stream_final_answer"):` branch that does `final_state["final_answer"] = streamed`, immediately followed by the comment `# Derive ok from final_state.error (...)` and the line `ok_flag = final_state.get("error") is None`. Insert the reconciliation BETWEEN the end of the stream/clarification/direct if-elif chain and that `ok_flag =` line. Re-grep before editing:
> `grep -n 'ok_flag = final_state.get("error") is None' services/orchestrator/main.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/services/orchestrator/test_main_final_answer_reconcile.py`:

```python
"""Final-answer reconciliation in _handle.

A goal whose skill returned ok=True (final_state["error"] is None) but whose
RENDERED final answer (post stream_final_answer) is a punt must be stored with
ok=False. Genuine successes and verified fixes stay ok=True. This is the third
reconciliation seam; it does not touch the skill-first / ReAct seams.
"""
from __future__ import annotations

import json

import pytest
from unittest.mock import AsyncMock

from services.orchestrator.main import (
    OrchestratorProcess,
    GOALS_STREAM,
    GOALS_GROUP,
)
from services.orchestrator import events as events_mod


def _make_storage():
    storage = AsyncMock()
    storage.workspaces = AsyncMock()
    storage.workspaces.record_session = AsyncMock()
    storage.workspaces.complete_session = AsyncMock()
    storage.workspaces.upsert_workspace = AsyncMock()
    return storage


def _stored_payload(proc):
    set_args = proc._redis.set.call_args[0]
    return json.loads(set_args[1])


@pytest.mark.asyncio
async def test_rendered_punt_flips_ok_to_false(monkeypatch):
    async def fake_emit(_type, **fields):
        pass
    monkeypatch.setattr(events_mod, "emit", fake_emit)

    proc = OrchestratorProcess()
    proc._redis = AsyncMock()

    # Skill path produced a clean state (no error) — the false-ok the A/B saw.
    orch = AsyncMock()
    orch.run_task.return_value = {
        "session_id": "s1",
        "goal_tree": {},
        "final_answer": "internal assembled answer",
        "error": None,
    }
    # The summarizer renders a PUNT into final_answer.
    orch.stream_final_answer = AsyncMock(
        return_value="I couldn't analyze the file because it is too large. Please share a snippet."
    )

    storage = _make_storage()
    payload = json.dumps({
        "task_id": "fa-punt-1",
        "task": "Find the bug in big_module.py",
        "session_id": "s1",
    })
    await proc._handle("800-0", {"payload": payload}, orch, storage)

    stored = _stored_payload(proc)
    assert stored["ok"] is False
    # The rendered punt is what was stored as the final answer.
    assert "too large" in stored["state"]["final_answer"].lower()
    # complete_session was told it failed.
    storage.workspaces.complete_session.assert_awaited()
    assert storage.workspaces.complete_session.call_args.kwargs.get("ok") is False
    proc._redis.xack.assert_awaited_once_with(GOALS_STREAM, GOALS_GROUP, "800-0")


@pytest.mark.asyncio
async def test_rendered_genuine_success_stays_ok_true(monkeypatch):
    async def fake_emit(_type, **fields):
        pass
    monkeypatch.setattr(events_mod, "emit", fake_emit)

    proc = OrchestratorProcess()
    proc._redis = AsyncMock()

    orch = AsyncMock()
    orch.run_task.return_value = {
        "session_id": "s1",
        "goal_tree": {},
        "final_answer": "internal",
        "error": None,
    }
    orch.stream_final_answer = AsyncMock(
        return_value="Here is the square function you asked for: def sq(x): return x*x"
    )

    storage = _make_storage()
    payload = json.dumps({
        "task_id": "fa-ok-1",
        "task": "Write a square function",
        "session_id": "s1",
    })
    await proc._handle("800-1", {"payload": payload}, orch, storage)

    stored = _stored_payload(proc)
    assert stored["ok"] is True


@pytest.mark.asyncio
async def test_preexisting_error_preserved_when_already_failed(monkeypatch):
    async def fake_emit(_type, **fields):
        pass
    monkeypatch.setattr(events_mod, "emit", fake_emit)

    proc = OrchestratorProcess()
    proc._redis = AsyncMock()

    orch = AsyncMock()
    orch.run_task.return_value = {
        "session_id": "s1",
        "goal_tree": {},
        "final_answer": "internal",
        "error": "1 subtask(s) failed: parse (error: boom)",
    }
    # Even a punt render: ok was already False; the original error stays.
    orch.stream_final_answer = AsyncMock(
        return_value="I could not process the file, it is too large."
    )

    storage = _make_storage()
    payload = json.dumps({
        "task_id": "fa-err-1",
        "task": "Parse the file",
        "session_id": "s1",
    })
    await proc._handle("800-2", {"payload": payload}, orch, storage)

    stored = _stored_payload(proc)
    assert stored["ok"] is False
    assert stored["state"]["error"] == "1 subtask(s) failed: parse (error: boom)"


@pytest.mark.asyncio
async def test_clarification_path_not_reconciled(monkeypatch):
    """Regression: a clarification question is not a punt-bearing answer path;
    awaiting_clarification states skip stream_final_answer and must not be
    flipped to ok=False by the new seam."""
    async def fake_emit(_type, **fields):
        pass
    monkeypatch.setattr(events_mod, "emit", fake_emit)

    proc = OrchestratorProcess()
    proc._redis = AsyncMock()

    orch = AsyncMock()
    orch.run_task.return_value = {
        "session_id": "s1",
        "goal_tree": {},
        "awaiting_clarification": True,
        "clarification_question": "Which file did you mean?",
        "error": None,
    }
    orch.stream_final_answer = AsyncMock(return_value="UNUSED")

    storage = _make_storage()
    payload = json.dumps({
        "task_id": "fa-clar-1",
        "task": "fix it",
        "session_id": "s1",
    })
    await proc._handle("800-3", {"payload": payload}, orch, storage)

    stored = _stored_payload(proc)
    assert stored["ok"] is True
    orch.stream_final_answer.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_main_final_answer_reconcile.py -v`
Expected: FAIL — `test_rendered_punt_flips_ok_to_false` asserts `stored["ok"] is False` but the current code stores `True` (the punt is never reconciled). The other three should already pass (they encode the regression-safe behavior).

- [ ] **Step 3: Write the minimal implementation**

In `services/orchestrator/main.py`, add the import near the other orchestrator imports (top of file, with the `from services.orchestrator...` group):

```python
from services.orchestrator.completion_guard import reconcile_final_answer
```

Then in `_handle`, insert the reconciliation immediately AFTER the stream/clarification/direct if-elif chain ends and BEFORE the `ok_flag = ...` line. Locate by the landmark comment `# Derive ok from final_state.error`:

```python
            # Reconcile the RENDERED final answer (post-summarizer) with ok/error.
            # The skill-first and ReAct seams reconcile their immediate outputs, but
            # the user-facing punt wording is produced downstream by the summarizer /
            # stream_final_answer and was never re-checked — so a "file too large /
            # provide a snippet" answer slipped through as ok=True (A/B report §8.3).
            # Setting final_state["error"] here propagates the downgrade to ok_flag
            # below, the stored result payload, the finally-block status, and
            # complete_session. Clarification states never reach here with a punt.
            if isinstance(final_state, dict) and not final_state.get("awaiting_clarification"):
                _rendered = final_state.get("final_answer", "") or ""
                _recon_ok, _recon_err, _ = reconcile_final_answer(
                    final_state.get("error") is None,
                    final_state.get("error"),
                    _rendered,
                )
                if not _recon_ok and final_state.get("error") is None:
                    final_state["error"] = _recon_err
            # Derive ok from final_state.error (FIX #2: failed subtasks now finalize with error set, not exception)
            ok_flag = final_state.get("error") is None
```

> Note: the existing line `ok_flag = final_state.get("error") is None` and its comment stay exactly as they are; the new block is inserted just above them. The clarification branch already set `final_answer` to the question and is excluded by the `not awaiting_clarification` guard, so a question is never treated as a punt.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_main_final_answer_reconcile.py -v`
Expected: PASS (all 4).

- [ ] **Step 5: Run the main-handler regression suite**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_main.py tests/services/orchestrator/test_main_direct_answer.py tests/services/orchestrator/test_main_clarification_no_guess.py -v`
Expected: PASS (no regressions — direct-answer and clarification paths unaffected; normal-path success stays ok=True).

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/main.py tests/services/orchestrator/test_main_final_answer_reconcile.py
git commit -m "fix(orchestrator): reconcile rendered final answer so a downstream punt flips ok=False

Wires reconcile_final_answer at the post-summarizer seam in _handle. Closes the
A/B report §8.3 false-ok where repo-fault-localize returned ok=True but the
rendered answer was 'file too large / provide a snippet'.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: BDD behavior spec for the rendered-answer seam

**Files:**
- Create: `tests/services/orchestrator/features/final_answer_reconcile.feature`
- Create: `tests/services/orchestrator/test_final_answer_reconcile_bdd.py`

**Interfaces:**
- Consumes: `OrchestratorProcess._handle` (driven end-to-end), `reconcile_final_answer` (indirectly), the shared `run_async` helper in `tests/conftest.py` (verified present, used by `test_ok_reconciliation_bdd.py`).
- Produces: a `@mocked` pytest-bdd scenario set proving a skill `ok=True` whose RENDERED final answer is a punt ends `ok=False` in the stored payload, while a genuine success stays `ok=True`.

- [ ] **Step 1: Write the failing feature file**

Create `tests/services/orchestrator/features/final_answer_reconcile.feature`:

```gherkin
@mocked
Feature: Reconcile the final rendered answer
  A goal whose skill returned ok=True but whose user-facing rendered final
  answer is a punt (e.g. "file too large / provide a snippet") must be stored
  with ok=False. Genuine successes and verified fixes stay ok=True. This is the
  third reconciliation seam, after the skill-first and ReAct finish seams.

  Background:
    Given an orchestrator handler with a mocked redis and storage
    And run_task returns a state with error None and final_answer "internal"

  Scenario: A rendered punt flips a false-ok to ok=False
    Given stream_final_answer renders "I couldn't analyze the file because it is too large. Please share a snippet."
    When the handler processes task "fa-bdd-punt"
    Then the stored result ok is False
    And the stored final answer contains "too large"

  Scenario: A genuine success answer stays ok=True
    Given stream_final_answer renders "Here is the square function you requested."
    When the handler processes task "fa-bdd-ok"
    Then the stored result ok is True

  Scenario: An unverified success claim is downgraded
    Given stream_final_answer renders "I fixed the bug and all tests pass."
    When the handler processes task "fa-bdd-claim"
    Then the stored result ok is False
```

- [ ] **Step 2: Write the failing step defs**

Create `tests/services/orchestrator/test_final_answer_reconcile_bdd.py`:

```python
# tests/services/orchestrator/test_final_answer_reconcile_bdd.py
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.main import OrchestratorProcess
from services.orchestrator import events as events_mod
from tests.conftest import run_async

scenarios("features/final_answer_reconcile.feature")


@pytest.fixture
def ctx(monkeypatch):
    async def _fake_emit(_type, **fields):
        pass
    monkeypatch.setattr(events_mod, "emit", _fake_emit)

    proc = OrchestratorProcess()
    proc._redis = AsyncMock()

    storage = AsyncMock()
    storage.workspaces = AsyncMock()
    storage.workspaces.record_session = AsyncMock()
    storage.workspaces.complete_session = AsyncMock()
    storage.workspaces.upsert_workspace = AsyncMock()

    orch = AsyncMock()
    orch.stream_final_answer = AsyncMock(return_value="")
    return {"proc": proc, "storage": storage, "orch": orch}


@given("an orchestrator handler with a mocked redis and storage")
def _handler(ctx):
    assert ctx["proc"]._redis is not None


@given("run_task returns a state with error None and final_answer \"internal\"")
def _state(ctx):
    ctx["orch"].run_task.return_value = {
        "session_id": "s1",
        "goal_tree": {},
        "final_answer": "internal",
        "error": None,
    }


@given(parsers.parse('stream_final_answer renders "{rendered}"'))
def _render(ctx, rendered):
    ctx["orch"].stream_final_answer = AsyncMock(return_value=rendered)


@when(parsers.parse('the handler processes task "{task_id}"'))
def _process(ctx, task_id):
    payload = json.dumps({"task_id": task_id, "task": "do the thing", "session_id": "s1"})
    run_async(ctx["proc"]._handle(f"{task_id}-msg", {"payload": payload}, ctx["orch"], ctx["storage"]))


@then(parsers.parse("the stored result ok is {value}"))
def _ok_is(ctx, value):
    set_args = ctx["proc"]._redis.set.call_args[0]
    stored = json.loads(set_args[1])
    assert stored["ok"] is (value == "True")


@then(parsers.parse('the stored final answer contains "{needle}"'))
def _answer_contains(ctx, needle):
    set_args = ctx["proc"]._redis.set.call_args[0]
    stored = json.loads(set_args[1])
    assert needle.lower() in stored["state"]["final_answer"].lower()
```

- [ ] **Step 3: Run the BDD scenarios to verify they fail (then pass after Task 2)**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_final_answer_reconcile_bdd.py -v`
Expected (if Task 2 is already merged): PASS for all three scenarios. (If running this task before Task 2's wiring, the punt + claim scenarios FAIL with `assert True is False` — that is the correct red state proving the BDD drives the same behavior the unit tests do.)

- [ ] **Step 4: Confirm the bdd marker is registered**

Run: `grep -n "bdd" pytest.ini`
Expected: a `bdd` marker line is present (the BDD foundation already registered it). No edit needed; if absent, add `bdd: behavior-driven scenarios` under `[pytest] markers`.

- [ ] **Step 5: Run the full orchestrator suite (regression gate)**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/ -q`
Expected: all green (the prior 684-passing baseline + the new unit/integration/BDD tests; zero failures, zero errors).

- [ ] **Step 6: Commit**

```bash
git add tests/services/orchestrator/features/final_answer_reconcile.feature tests/services/orchestrator/test_final_answer_reconcile_bdd.py
git commit -m "test(bdd): final-answer reconciliation scenarios for the rendered-answer seam

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Behavior (BDD) — Gherkin

The full `.feature` is created in Task 3 (`tests/services/orchestrator/features/final_answer_reconcile.feature`). Reproduced here as the behavior contract:

```gherkin
@mocked
Feature: Reconcile the final rendered answer
  A goal whose skill returned ok=True but whose user-facing rendered final
  answer is a punt (e.g. "file too large / provide a snippet") must be stored
  with ok=False. Genuine successes and verified fixes stay ok=True. This is the
  third reconciliation seam, after the skill-first and ReAct finish seams.

  Background:
    Given an orchestrator handler with a mocked redis and storage
    And run_task returns a state with error None and final_answer "internal"

  Scenario: A rendered punt flips a false-ok to ok=False
    Given stream_final_answer renders "I couldn't analyze the file because it is too large. Please share a snippet."
    When the handler processes task "fa-bdd-punt"
    Then the stored result ok is False
    And the stored final answer contains "too large"

  Scenario: A genuine success answer stays ok=True
    Given stream_final_answer renders "Here is the square function you requested."
    When the handler processes task "fa-bdd-ok"
    Then the stored result ok is True

  Scenario: An unverified success claim is downgraded
    Given stream_final_answer renders "I fixed the bug and all tests pass."
    When the handler processes task "fa-bdd-claim"
    Then the stored result ok is False
```

---

## Self-Review

**1. Spec coverage**
- Reuse `completion_guard`, no new punt-phrase list → Task 1 composes `reconcile_ok`/`is_punt_answer`; no new list. ✓
- Apply to the FINAL RENDERED answer after the summarizer → Task 2 wires after `stream_final_answer` write-back. ✓
- Punt flips ok/status to NOT-success → Task 2 sets `final_state["error"]`, which drives `ok_flag`, the payload, the `finally` status, and `complete_session`. ✓
- Corrected ok lands in the Redis `labmate:result:<task_id>` payload → asserted in `test_rendered_punt_flips_ok_to_false` (reads `proc._redis.set` payload). ✓
- Do NOT regress seams `_run_skill_first` / ReAct finish → both untouched; this is additive (third seam). ✓
- Pure logic stays pure + unit-tested → Task 1 helper is side-effect free with a purity test. ✓
- Genuine success stays ok=True / verified fix stays ok=True → `test_final_genuine_success_answer_unchanged`, `test_final_verified_fix_stays_ok`, `test_rendered_genuine_success_stays_ok_true`. ✓
- BDD: skill ok=True but rendered answer is a punt → ok=False → Task 3 scenario 1. ✓
- CLAUDE.md: no model call added (pure logic); stdout-sacred preserved (no print); `thinking_budget`/`api_key` untouched. ✓

**2. Placeholder scan** — No TBD/TODO/"handle edge cases"/"similar to". Every code step has full code; every command has expected output. ✓

**3. Type consistency** — `reconcile_final_answer(ok: bool, error: str | None, answer: str, *, tests_passed: bool = False) -> tuple[bool, str | None, str]` is defined identically in Task 1's interface block, its implementation, its unit tests, and its call site in Task 2. The call site passes `(final_state.get("error") is None, final_state.get("error"), _rendered)` matching `(ok, error, answer)`. Return is unpacked as `(_recon_ok, _recon_err, _)`. ✓
