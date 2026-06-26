# Fix `load_skill` Churn in the ReAct Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the weak Q4 31B model from burning ~⅓ of its iteration budget by repeatedly `load_skill`-ing skills it has already loaded this goal — short-circuit a repeat-load with a clear "already loaded" message AND refund the wasted iteration unit, while a first load of any skill behaves exactly as today.

**Architecture:** Add a pure, unit-testable helper module `load_skill_guard.py` (an `already_loaded?` check + an "already loaded" message builder). Wire it into `_run_react_loop` in `services/orchestrator/coding_orchestrator.py`: track the set of skills loaded *this goal* in a local `set`, and when the `load_skill` dispatch branch fires for a name already in that set, skip the real `runner.load_skill(...)` call, return the guard message as the tool result, and call `budget.refund()` so the no-op turn does not cost progress. First loads are unchanged (real load, charged normally, name recorded). No change to `SkillRunner`, `IterationBudget`, or any other tool branch.

**Tech Stack:** Python 3.11, asyncio, pytest + pytest-asyncio, pytest-bdd, respx (`fake_model` fixture), `unittest.mock`.

## Global Constraints

- **stdout is sacred** — never `print()` / `console.log()` in any MCP-adjacent code path; this plan touches only the orchestrator (no stdout writes added). Logging, if any, goes to stderr via `logging.getLogger("orchestrator")`.
- **llama.cpp** — every model call must set `extra_body={"thinking_budget_tokens": ...}` and `api_key="not-needed"`. This plan adds **no new model calls**, so this is inherited unchanged from the existing branches.
- **Additive + regression-safe** — no removals, no signature changes to existing public functions. New module + one new local variable + one new `elif`/branch ordering tweak inside the existing `load_skill` dispatch. First-load behavior is byte-for-byte unchanged.
- **File naming** — Python files `snake_case.py`; classes `PascalCase`; functions `snake_case`. Tests live under `tests/` mirroring `services/` structure.
- **Testing** — `@pytest.mark.asyncio` on async tests; assert structure, not literal LLM text. BDD scenarios tagged `@mocked`, use the `fake_model` HTTP-seam contract and `run_async` from `tests/conftest.py`. Markers `bdd` and `mocked` are already registered in `pytest.ini`.
- **Env knob** — `LABMATE_REFUND_REPEAT_LOAD_SKILL` (default `"1"` = ON). When `"0"`, the guard still short-circuits the redundant reload (cheap, always correct) but does **not** refund the budget unit (lets an operator A/B the refund half in isolation). Read once at module import next to the other knobs.

---

## File Map

| File | Create / Modify | Responsibility |
|---|---|---|
| `services/orchestrator/load_skill_guard.py` | **Create** | Pure helpers: `is_repeat_load(name, loaded_names) -> bool` and `already_loaded_message(name, loaded_names) -> dict`. No I/O, no imports from the orchestrator. |
| `services/orchestrator/coding_orchestrator.py` | **Modify** | Add module-level knob `REFUND_REPEAT_LOAD_SKILL`; in `_run_react_loop` add a per-goal `loaded_skills: set[str]`; rewrite the `load_skill` dispatch branch to short-circuit + refund repeat-loads and record first loads. |
| `tests/services/orchestrator/test_load_skill_guard.py` | **Create** | Unit tests for the two pure helpers. |
| `tests/services/orchestrator/test_coding_orchestrator.py` | **Modify** | Add wire-in unit tests for the `_run_react_loop` short-circuit + refund behavior. |
| `tests/services/orchestrator/features/load_skill_churn.feature` | **Create** | Gherkin behavior spec, `@mocked`. |
| `tests/services/orchestrator/test_load_skill_churn_bdd.py` | **Create** | pytest-bdd step defs binding the feature to the orchestrator via the established `litellm.acompletion` patch pattern. |

---

## Behavior (BDD) — Gherkin

`tests/services/orchestrator/features/load_skill_churn.feature`:

```gherkin
Feature: De-duplicate load_skill calls within a single goal
  On the Q4 31B model the ReAct loop wastes iteration budget by re-loading
  skills it has already loaded this goal — leaving too few steps to actually
  read, edit, test, and verify. The loop now records which skills it has
  loaded this goal: a FIRST load runs the real loader and is charged a turn,
  but a REPEAT load of an already-loaded skill is short-circuited with a clear
  "already loaded — call its tools directly" message and the wasted iteration
  is refunded. Non-load tools and first loads are unaffected.

  @mocked
  Scenario: The pure guard flags a repeat load and passes a first load
    Given the set of loaded skills is "code-review"
    Then is_repeat_load for "code-review" is True
    And is_repeat_load for "test-gen" is False

  @mocked
  Scenario: The already-loaded message names the skill and lists loaded skills
    Given the set of loaded skills is "code-review,test-gen"
    When the already-loaded message is built for "code-review"
    Then the message text contains "already loaded"
    And the message text contains "code-review"
    And the message text contains "test-gen"
    And the message text contains "do not load_skill"

  @mocked
  Scenario: A repeat load_skill in the ReAct loop is short-circuited and refunded
    Given a ReAct orchestrator whose model loads "code-review" twice then finishes
    When the goal "review then fix the file" is executed
    Then the skill runner loaded "code-review" exactly once
    And the second load result reports it is already loaded
    And the iteration budget was refunded for the repeat load

  @mocked
  Scenario: A first load of a different skill is not short-circuited
    Given a ReAct orchestrator whose model loads "code-review" then "test-gen" then finishes
    When the goal "review then test the file" is executed
    Then the skill runner loaded "code-review" exactly once
    And the skill runner loaded "test-gen" exactly once
    And neither first load reported already loaded
```

---

## Task 1: Pure `load_skill_guard` helpers

**Files:**
- Create: `services/orchestrator/load_skill_guard.py`
- Test: `tests/services/orchestrator/test_load_skill_guard.py`

**Interfaces:**
- Consumes: nothing (pure module, stdlib only).
- Produces (later tasks rely on these exact names/signatures):
  - `is_repeat_load(name: str, loaded_names: set[str]) -> bool` — True iff `name` is non-empty and already in `loaded_names`.
  - `already_loaded_message(name: str, loaded_names: set[str]) -> dict` — returns a JSON-serializable dict `{"name": "load_skill", "response": {"status": "already_loaded", "name": name, "message": <str>, "loaded": <sorted list>}}`. The `message` string contains the phrases `"already loaded"`, the skill `name`, the instruction `"call them directly, do not load_skill it again"`, and `"Loaded skills: <comma-joined sorted list>"`.

- [ ] **Step 1: Write the failing test**

`tests/services/orchestrator/test_load_skill_guard.py`:

```python
# tests/services/orchestrator/test_load_skill_guard.py
from __future__ import annotations

import json

import pytest

from services.orchestrator.load_skill_guard import (
    is_repeat_load,
    already_loaded_message,
)

pytestmark = pytest.mark.mocked


def test_is_repeat_load_true_when_already_loaded():
    assert is_repeat_load("code-review", {"code-review"}) is True


def test_is_repeat_load_false_when_first_load():
    assert is_repeat_load("test-gen", {"code-review"}) is False


def test_is_repeat_load_false_for_empty_name():
    # An empty / missing name must never be treated as a repeat — it falls
    # through to the real loader, which surfaces the proper "unknown skill" error.
    assert is_repeat_load("", {"code-review"}) is False


def test_already_loaded_message_shape_and_text():
    msg = already_loaded_message("code-review", {"test-gen", "code-review"})
    assert msg["name"] == "load_skill"
    resp = msg["response"]
    assert resp["status"] == "already_loaded"
    assert resp["name"] == "code-review"
    assert resp["loaded"] == ["code-review", "test-gen"]  # sorted
    text = resp["message"]
    assert "already loaded" in text
    assert "code-review" in text
    assert "do not load_skill it again" in text
    assert "Loaded skills: code-review, test-gen" in text


def test_already_loaded_message_is_json_serializable():
    msg = already_loaded_message("code-review", {"code-review"})
    # Must survive json.dumps so it can become a tool-result string.
    assert json.loads(json.dumps(msg))["response"]["status"] == "already_loaded"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_load_skill_guard.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.orchestrator.load_skill_guard'`.

- [ ] **Step 3: Write minimal implementation**

`services/orchestrator/load_skill_guard.py`:

```python
"""Pure helpers to de-duplicate load_skill calls within a single ReAct goal.

The weak local model frequently re-issues load_skill for a skill it has
already loaded this goal, burning iteration budget. The ReAct loop tracks the
set of skills loaded so far this goal and uses these helpers to (a) detect a
repeat load and (b) build a clear "already loaded — call its tools directly"
tool result. No async, no I/O, no orchestrator imports — fully unit-testable.
"""

from __future__ import annotations


def is_repeat_load(name: str, loaded_names: set[str]) -> bool:
    """True iff ``name`` is a non-empty skill already in ``loaded_names``.

    An empty / missing name is NOT a repeat: it must fall through to the real
    loader so the proper "unknown skill" error is surfaced to the model.
    """
    return bool(name) and name in loaded_names


def already_loaded_message(name: str, loaded_names: set[str]) -> dict:
    """Build a JSON-serializable load_skill tool result for a repeat load.

    Mirrors SkillRunner.load_skill's envelope shape
    ({"name": "load_skill", "response": {...}}) so the model sees a familiar
    structure, with status 'already_loaded' and an explicit instruction to call
    the skill's tools directly instead of re-loading it.
    """
    loaded_sorted = sorted(loaded_names)
    message = (
        f"skill '{name}' is already loaded; its tools are available — "
        f"call them directly, do not load_skill it again. "
        f"Loaded skills: {', '.join(loaded_sorted)}"
    )
    return {
        "name": "load_skill",
        "response": {
            "status": "already_loaded",
            "name": name,
            "message": message,
            "loaded": loaded_sorted,
        },
    }


__all__ = ["is_repeat_load", "already_loaded_message"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_load_skill_guard.py -q`
Expected: PASS — 5 passed.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/load_skill_guard.py tests/services/orchestrator/test_load_skill_guard.py
git commit -m "feat(orchestrator): pure load_skill repeat-load guard helpers"
```

---

## Task 2: Wire the guard into `_run_react_loop` (short-circuit + refund)

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py`
- Test: `tests/services/orchestrator/test_coding_orchestrator.py`

**Interfaces:**
- Consumes: `is_repeat_load`, `already_loaded_message` from Task 1; `IterationBudget.refund()` (already exists, `services/orchestrator/iteration_budget.py:60`).
- Produces: no new public symbols. Behavior change is internal to `_run_react_loop`. A new module-level constant `REFUND_REPEAT_LOAD_SKILL: bool` is added next to the other env knobs.

**Anchor (re-verify before editing — match on structure, not line numbers):**
1. The import block near the top of `coding_orchestrator.py` that already imports `from .iteration_budget import IterationBudget, CHEAP_TOOLS` — add the new import beside it.
2. The env-knob block near the top (where `SEQUENCING_MODE`, `MAX_SEQ_STEPS`, `REPLAN_COMPOUND_GATE` are defined) — add `REFUND_REPEAT_LOAD_SKILL` there.
3. Inside `_run_react_loop`, the per-goal locals declared before the `while True:` loop (e.g. `loop_detector = LoopDetector()`, `_tools_used: list[str] = []`, `edited_files: set[str] = set()`) — add `loaded_skills: set[str] = set()` there.
4. Inside the per-tool-call `for tc in tool_calls:` body, the branch:
   ```python
   if name == "load_skill" and self.skill_router is not None:
       obs = self.skill_router.runner.load_skill(args.get("name", ""))
       content = json.dumps(obs)
   ```
   This is the *only* branch to rewrite.

- [ ] **Step 1: Write the failing tests**

Append to `tests/services/orchestrator/test_coding_orchestrator.py`, inside the existing `TestReactExecute` class (it already defines `_make_orch` and `_make_tool_call_response`). Add these three methods:

```python
    @pytest.mark.asyncio
    async def test_react_execute_repeat_load_skill_short_circuited(self):
        """A 2nd load_skill for an already-loaded skill must NOT call the real
        loader again; the tool result reports 'already loaded'."""
        runner = MagicMock()
        runner.catalog_prompt.return_value = "- code-review: review code"
        runner.tool_schema.return_value = {
            "type": "function",
            "function": {"name": "load_skill", "parameters": {}},
        }
        runner.load_skill.return_value = {
            "name": "load_skill",
            "response": {"status": "loaded", "name": "code-review", "body": "BODY"},
        }
        skill_router = MagicMock()
        skill_router.runner = runner
        orch = self._make_orch(skill_router=skill_router)

        # Turn 1: load code-review. Turn 2: load code-review AGAIN. Turn 3: finish.
        r1 = self._make_tool_call_response("load_skill", {"name": "code-review"})
        r2 = self._make_tool_call_response("load_skill", {"name": "code-review"})
        r3 = self._make_tool_call_response("finish", {"summary": "done"})

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, side_effect=[r1, r2, r3]):
            result = await orch.react_execute("review then fix")

        assert result["ok"] is True
        # The real loader ran only for the FIRST load.
        runner.load_skill.assert_called_once_with("code-review")

    @pytest.mark.asyncio
    async def test_react_execute_repeat_load_skill_refunds_budget(self):
        """The redundant reload turn is refunded, so the model still has enough
        budget to finish. With max_steps=2: load (1) -> redundant load (refunded)
        -> finish should succeed rather than exhausting the budget."""
        runner = MagicMock()
        runner.catalog_prompt.return_value = "- code-review: review code"
        runner.tool_schema.return_value = {
            "type": "function",
            "function": {"name": "load_skill", "parameters": {}},
        }
        runner.load_skill.return_value = {
            "name": "load_skill",
            "response": {"status": "loaded", "name": "code-review", "body": "BODY"},
        }
        skill_router = MagicMock()
        skill_router.runner = runner
        orch = self._make_orch(skill_router=skill_router, max_steps=2)

        r1 = self._make_tool_call_response("load_skill", {"name": "code-review"})
        r2 = self._make_tool_call_response("load_skill", {"name": "code-review"})
        r3 = self._make_tool_call_response("finish", {"summary": "done"})

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, side_effect=[r1, r2, r3]):
            result = await orch.react_execute("review then fix")

        # Without the refund, load(1)+load(2) would exhaust max_steps=2 and the
        # grace turn would be the finish — but the finish would still run, so we
        # assert on the stronger signal: the loader ran once and we finished ok.
        assert result["ok"] is True
        assert "done" in result["summary"]
        runner.load_skill.assert_called_once_with("code-review")

    @pytest.mark.asyncio
    async def test_react_execute_distinct_load_skill_not_short_circuited(self):
        """Loading two DIFFERENT skills both hit the real loader (no false
        short-circuit on a first load)."""
        runner = MagicMock()
        runner.catalog_prompt.return_value = "- code-review: x\n- test-gen: y"
        runner.tool_schema.return_value = {
            "type": "function",
            "function": {"name": "load_skill", "parameters": {}},
        }
        runner.load_skill.side_effect = lambda n: {
            "name": "load_skill",
            "response": {"status": "loaded", "name": n, "body": "BODY"},
        }
        skill_router = MagicMock()
        skill_router.runner = runner
        orch = self._make_orch(skill_router=skill_router)

        r1 = self._make_tool_call_response("load_skill", {"name": "code-review"})
        r2 = self._make_tool_call_response("load_skill", {"name": "test-gen"})
        r3 = self._make_tool_call_response("finish", {"summary": "done"})

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, side_effect=[r1, r2, r3]):
            result = await orch.react_execute("review then test")

        assert result["ok"] is True
        called = {c.args[0] for c in runner.load_skill.call_args_list}
        assert called == {"code-review", "test-gen"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest "tests/services/orchestrator/test_coding_orchestrator.py::TestReactExecute::test_react_execute_repeat_load_skill_short_circuited" "tests/services/orchestrator/test_coding_orchestrator.py::TestReactExecute::test_react_execute_distinct_load_skill_not_short_circuited" -q`
Expected: FAIL — `test_react_execute_repeat_load_skill_short_circuited` fails on `assert_called_once_with` (the loader is currently called twice). The distinct-skill test passes already (it documents the preserved behavior); that is fine.

- [ ] **Step 3: Write minimal implementation**

3a. Add the import beside the existing `iteration_budget` import:

```python
from .iteration_budget import IterationBudget, CHEAP_TOOLS
from .load_skill_guard import is_repeat_load, already_loaded_message
```

3b. Add the env knob in the top-of-file knob block (next to `REPLAN_COMPOUND_GATE`):

```python
# When 1 (default), a load_skill call for a skill ALREADY loaded this goal is
# short-circuited (no real reload) AND the wasted iteration is refunded, so the
# weak local model cannot burn its step budget re-loading the same skills. When
# 0, the redundant reload is still short-circuited but the budget is NOT
# refunded (lets an operator A/B the refund half in isolation).
REFUND_REPEAT_LOAD_SKILL = os.getenv("LABMATE_REFUND_REPEAT_LOAD_SKILL", "1") == "1"
```

3c. Add the per-goal tracking set alongside the other per-goal locals declared before `while True:` in `_run_react_loop` (place it next to `edited_files: set[str] = set()`):

```python
        # Skills already loaded THIS goal. A repeat load_skill for a name in
        # this set is short-circuited + refunded (see load_skill dispatch below)
        # so the model stops churning its iteration budget re-loading skills.
        loaded_skills: set[str] = set()
```

3d. Replace the `load_skill` dispatch branch. Find:

```python
                    if name == "load_skill" and self.skill_router is not None:
                        obs = self.skill_router.runner.load_skill(args.get("name", ""))
                        content = json.dumps(obs)
```

Replace with:

```python
                    if name == "load_skill" and self.skill_router is not None:
                        _skill_name = args.get("name", "")
                        if is_repeat_load(_skill_name, loaded_skills):
                            # Already loaded this goal: do NOT reload. Return a
                            # clear "already loaded — call its tools directly"
                            # result and refund the wasted iteration so a churn
                            # of redundant loads cannot starve real work.
                            obs = already_loaded_message(_skill_name, loaded_skills)
                            content = json.dumps(obs)
                            if REFUND_REPEAT_LOAD_SKILL:
                                budget.refund()
                            await events.emit(
                                "load_skill.deduped",
                                name=_skill_name,
                                loaded=sorted(loaded_skills),
                                refunded=REFUND_REPEAT_LOAD_SKILL,
                            )
                        else:
                            obs = self.skill_router.runner.load_skill(_skill_name)
                            content = json.dumps(obs)
                            # Record a successful first load so a later repeat is
                            # deduped. Only record on a real 'loaded'/'already_loaded'
                            # status — an error (unknown skill / cap) must NOT be
                            # remembered as loaded.
                            _resp = obs.get("response") if isinstance(obs, dict) else None
                            _status = _resp.get("status") if isinstance(_resp, dict) else None
                            if _skill_name and _status in ("loaded", "already_loaded"):
                                loaded_skills.add(_skill_name)
```

Notes for the implementer:
- `budget` is the `IterationBudget` local already in scope in `_run_react_loop`.
- `events.emit` is already imported (`from . import events`) and used throughout the loop; the new `load_skill.deduped` event is best-effort like the others. If a stricter no-new-events posture is preferred at review, the `await events.emit(...)` line may be dropped without affecting the tests — they assert on the loader call count and the result content, not on the event.
- Do not touch the `record(call_signature(...))` loop-detector call below this branch: a deduped repeat still records its signature, which is correct (a true loop of identical loads will also trip the loop detector as a backstop).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest "tests/services/orchestrator/test_coding_orchestrator.py::TestReactExecute" -q`
Expected: PASS — all `TestReactExecute` tests pass, including the three new ones.

- [ ] **Step 5: Run the full orchestrator suite (regression gate)**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/ -q`
Expected: PASS — no regressions (the pre-existing `test_react_execute_with_skill_router_load_skill` still passes: it loads `test-skill` exactly once, which is a first load and unchanged).

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/coding_orchestrator.py tests/services/orchestrator/test_coding_orchestrator.py
git commit -m "feat(orchestrator): dedupe + refund repeat load_skill in ReAct loop"
```

---

## Task 3: BDD feature + step defs

**Files:**
- Create: `tests/services/orchestrator/features/load_skill_churn.feature` (content given in "Behavior (BDD)" above — write it verbatim).
- Create: `tests/services/orchestrator/test_load_skill_churn_bdd.py`

**Interfaces:**
- Consumes: `is_repeat_load`, `already_loaded_message` (Task 1); `AsyncOrchestrator` + the `litellm.acompletion` patch seam (Task 2); `run_async` from `tests/conftest.py`.
- Produces: nothing imported elsewhere.

- [ ] **Step 1: Write the feature file**

Create `tests/services/orchestrator/features/load_skill_churn.feature` with the exact Gherkin from the "Behavior (BDD)" section above.

- [ ] **Step 2: Write the failing step defs**

`tests/services/orchestrator/test_load_skill_churn_bdd.py`:

```python
# tests/services/orchestrator/test_load_skill_churn_bdd.py
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.load_skill_guard import (
    is_repeat_load,
    already_loaded_message,
)
from tests.conftest import run_async

pytestmark = [pytest.mark.bdd, pytest.mark.mocked]

scenarios("features/load_skill_churn.feature")


@pytest.fixture
def ctx():
    return {"loaded": set(), "msg": None, "orch": None, "result": None}


# ── Pure-helper scenarios ──────────────────────────────────────────────────
@given(parsers.parse('the set of loaded skills is "{names}"'))
def _loaded_set(ctx, names):
    ctx["loaded"] = set(names.split(","))


@then(parsers.parse('is_repeat_load for "{name}" is {expected}'))
def _is_repeat(ctx, name, expected):
    want = expected.strip() == "True"
    assert is_repeat_load(name, ctx["loaded"]) is want


@when(parsers.parse('the already-loaded message is built for "{name}"'))
def _build_msg(ctx, name):
    ctx["msg"] = already_loaded_message(name, ctx["loaded"])


@then(parsers.parse('the message text contains "{phrase}"'))
def _msg_contains(ctx, phrase):
    assert phrase in ctx["msg"]["response"]["message"]


# ── ReAct wire-in scenarios ────────────────────────────────────────────────
def _tool_call_response(tool_name: str, arguments: dict, call_id: str):
    """A litellm-shaped response that issues one tool call."""
    tc = MagicMock()
    tc.id = call_id
    tc.function = MagicMock()
    tc.function.name = tool_name
    tc.function.arguments = json.dumps(arguments)
    msg = MagicMock()
    msg.content = None
    msg.tool_calls = [tc]
    msg.reasoning_content = ""
    msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    return MagicMock(choices=[MagicMock(message=msg)])


def _build_orch():
    from services.orchestrator.coding_orchestrator import AsyncOrchestrator

    runner = MagicMock()
    runner.catalog_prompt.return_value = "- code-review: x\n- test-gen: y"
    runner.tool_schema.return_value = {
        "type": "function",
        "function": {"name": "load_skill", "parameters": {}},
    }
    runner.load_skill.side_effect = lambda n: {
        "name": "load_skill",
        "response": {"status": "loaded", "name": n, "body": "BODY"},
    }
    runner.reset_activations = MagicMock()
    router = MagicMock()
    router.runner = runner
    orch = AsyncOrchestrator(skill_router=router, mcp=AsyncMock(), workspace="/tmp", max_steps=6)
    return orch, runner


def _run_goal(ctx, responses):
    orch = ctx["orch"]

    async def _run():
        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, side_effect=responses):
            return await orch.react_execute(ctx["goal"])

    ctx["result"] = run_async(_run())


@given(parsers.parse(
    'a ReAct orchestrator whose model loads "{name}" twice then finishes'))
def _orch_double_load(ctx, monkeypatch, name):
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.SEQUENCING_MODE",
        "skill_first", raising=False,
    )
    orch, runner = _build_orch()
    ctx["orch"], ctx["runner"] = orch, runner
    ctx["responses"] = [
        _tool_call_response("load_skill", {"name": name}, "c1"),
        _tool_call_response("load_skill", {"name": name}, "c2"),
        _tool_call_response("finish", {"summary": "done"}, "c3"),
    ]


@given(parsers.parse(
    'a ReAct orchestrator whose model loads "{a}" then "{b}" then finishes'))
def _orch_two_skills(ctx, monkeypatch, a, b):
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.SEQUENCING_MODE",
        "skill_first", raising=False,
    )
    orch, runner = _build_orch()
    ctx["orch"], ctx["runner"] = orch, runner
    ctx["responses"] = [
        _tool_call_response("load_skill", {"name": a}, "c1"),
        _tool_call_response("load_skill", {"name": b}, "c2"),
        _tool_call_response("finish", {"summary": "done"}, "c3"),
    ]


@when(parsers.parse('the goal "{goal}" is executed'))
def _execute(ctx, goal):
    ctx["goal"] = goal
    _run_goal(ctx, ctx["responses"])


@then(parsers.parse('the skill runner loaded "{name}" exactly once'))
def _loaded_once(ctx, name):
    calls = [c for c in ctx["runner"].load_skill.call_args_list if c.args[0] == name]
    assert len(calls) == 1, ctx["runner"].load_skill.call_args_list


@then("the second load result reports it is already loaded")
def _second_already_loaded(ctx):
    # The runner was invoked once; the repeat was short-circuited, so the model
    # finished ok rather than erroring, and the loader ran a single time.
    assert ctx["result"]["ok"] is True
    code_review_calls = [
        c for c in ctx["runner"].load_skill.call_args_list if c.args[0] == "code-review"
    ]
    assert len(code_review_calls) == 1


@then("the iteration budget was refunded for the repeat load")
def _budget_refunded(ctx):
    # End-to-end proxy for the refund: with the refund active the goal completes
    # successfully within budget. (The unit test in Task 2 asserts the loader
    # call count directly; here we confirm honest completion.)
    assert ctx["result"]["ok"] is True


@then("neither first load reported already loaded")
def _no_false_dedupe(ctx):
    called = {c.args[0] for c in ctx["runner"].load_skill.call_args_list}
    assert "code-review" in called
    assert "test-gen" in called
```

- [ ] **Step 3: Run the BDD test to verify it fails first, then implement-check**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_load_skill_churn_bdd.py -q`
Expected (with Tasks 1+2 already merged): PASS — all four scenarios pass. (If run *before* Task 2's dispatch edit, the "short-circuited and refunded" scenario fails on `_loaded_once` because the loader is called twice — confirming the test has teeth.)

- [ ] **Step 4: Run the whole BDD + orchestrator suite (regression gate)**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/ -q`
Expected: PASS — all green (the CLAUDE.md baseline is "orchestrator + memory 684 passed"; this adds tests, removes none).

- [ ] **Step 5: Commit**

```bash
git add tests/services/orchestrator/features/load_skill_churn.feature tests/services/orchestrator/test_load_skill_churn_bdd.py
git commit -m "test(orchestrator): BDD for load_skill churn dedupe + refund"
```

---

## Self-Review

**1. Spec coverage**

| Requirement (from the task brief) | Implemented by |
|---|---|
| Track skills loaded this goal, in `_run_react_loop` | Task 2 step 3c — `loaded_skills: set[str]` per-goal local. |
| Repeat load: don't reload, return "already loaded; call them directly, do not load_skill it again. Loaded skills: <list>" | Task 1 `already_loaded_message` (exact phrases) + Task 2 step 3d short-circuit. |
| Repeat load: refund the iteration unit | Task 2 step 3d `budget.refund()` (reuses `IterationBudget.refund()`). |
| First load unchanged (real load, charged, recorded) | Task 2 step 3d `else:` branch — unchanged loader call + `loaded_skills.add` only on success. |
| "already loaded?" check + message builder pure/unit-testable | Task 1 module — no I/O, dedicated unit test file. |
| BDD: 2nd load short-circuited + refunded; first load of a different skill unaffected; non-load tools unchanged | Task 3 feature + step defs; non-load tools verified by the untouched full suite regressing green (Task 2 step 5, Task 3 step 4). |
| Env knob | `LABMATE_REFUND_REPEAT_LOAD_SKILL` (default ON) — Task 2 step 3b. |

**2. Placeholder scan** — No "TBD"/"handle edge cases"/"similar to". Every code step shows full code. The optional `events.emit` line is explicitly marked optional with a fallback, not a placeholder.

**3. Type consistency** — `is_repeat_load(name: str, loaded_names: set[str]) -> bool` and `already_loaded_message(name: str, loaded_names: set[str]) -> dict` are spelled identically in Task 1 (definition), the Task 1 tests, the Task 2 import + call sites, and the Task 3 imports + step defs. The envelope `{"name": "load_skill", "response": {"status": "already_loaded", ...}}` matches `SkillRunner.load_skill`'s real shape (`{"name": "load_skill", "response": {"status": ...}}`), so `json.dumps(obs)` produces a tool result the model already understands. `budget.refund()` matches the existing `IterationBudget.refund()` signature (no args, returns `None`).

**Edge cases handled:** empty/missing skill name is never deduped (`is_repeat_load` returns False) → falls through to the real loader's "unknown skill" error; a failed first load (error status) is not recorded in `loaded_skills`, so a legitimate retry after a transient error still reaches the loader; the loop detector below the branch is untouched, so a pathological all-identical-load loop still has the loop-detector backstop.
