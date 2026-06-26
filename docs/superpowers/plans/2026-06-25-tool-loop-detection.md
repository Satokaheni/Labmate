# Tool-Loop / No-Progress Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect when the ReAct executor repeats or cycles the same tool calls and break the loop early with an honest failure instead of burning all `max_steps`.

**Architecture:** A pure, dependency-free `LoopDetector` class in a new module `services/orchestrator/loop_detection.py` ingests a deterministic signature for each `(tool_name, normalized_arguments)` call and reports `should_break`. The ReAct loop in `AsyncOrchestrator.react_execute` feeds every tool call into one detector instance per goal; on trip it emits a `loop.detected` event, logs to stderr, and returns `{"ok": False, "summary": ...}`. The detector is conservative: it only trips on an *immediate consecutive* repeat of one signature OR a short *cycle* of signatures with no new signature appearing — distinct calls (e.g. reads of different paths) never trip it.

**Tech Stack:** Python 3.11+, `json.dumps(..., sort_keys=True)` for deterministic argument normalization, pytest + pytest-asyncio for unit tests, pytest-bdd for the behavior contract, `respx`/`fake_model` for the wire-in scenario.

## Global Constraints

- **No model coupling in the detector:** `loop_detection.py` MUST be pure — no LLM calls, no async, no Redis, no imports from `coding_orchestrator`, `events`, or `litellm`. Core logic is synchronous and unit-testable in isolation.
- **Deterministic argument normalization:** signatures use `json.dumps(args, sort_keys=True, default=str)`; key order must never affect the signature.
- **Regression-safe wire-in:** when no repetition occurs, `react_execute` behavior is byte-for-byte unchanged. Every existing test in `tests/services/orchestrator/test_coding_orchestrator.py` MUST still pass.
- **Env knob:** repeat threshold is read once at module load via `LOOP_REPEAT_LIMIT = int(os.getenv("LOOP_REPEAT_LIMIT", "2"))`, matching the existing knob style in `services/orchestrator/graph.py` (module-level `os.getenv` with a typed cast). Default is `2`.
- **Additive State/config only:** do NOT remove or rename any field in `services/orchestrator/types.py`. No new `State` field is required by this feature; if one is added it must be additive and `total=False`.
- **`finish` is never counted:** the `finish` tool returns from the loop before any detector update, so it can never trip detection. Only dispatched tools (`load_skill`, `call_skill_tool`, `run_bash`, `read_file`, `write_file`, `list_dir`, `code_semantic_search`, local tools) feed the detector.
- **stderr only:** the detector and wire-in log via `logging` to stderr — never `print`/stdout (MCP stdout rule).
- **BDD scenarios tagged `@mocked`:** they must run in CI with no GPU and no live inference server.
- **Assume the foundation harness exists:** a separate foundation plan provides `tests/conftest.py` with a `fake_model` respx fixture and registers the pytest-bdd plugin. This plan consumes those; it does not create them.

---

## File Map

| File | Create/Modify | Responsibility |
|------|---------------|----------------|
| `services/orchestrator/loop_detection.py` | Create | Pure `LoopDetector` class + `call_signature()` helper + `LOOP_REPEAT_LIMIT` env knob. No I/O, no async. |
| `services/orchestrator/coding_orchestrator.py` | Modify (`react_execute`, lines ~412–614) | Instantiate one `LoopDetector` per goal; feed each dispatched tool call's signature; on `should_break` emit `loop.detected`, log, and return an honest failure. |
| `tests/services/orchestrator/test_loop_detection.py` | Create | Unit TDD for `call_signature()` + `LoopDetector` (repeats trip, distinct calls don't, cycles trip, threshold configurable, reset). |
| `tests/services/orchestrator/features/tool_loop_detection.feature` | Create | Gherkin behavior contract, scenarios tagged `@mocked`. |
| `tests/services/orchestrator/test_tool_loop_detection_bdd.py` | Create | pytest-bdd step defs binding the feature to `LoopDetector` and to a `react_execute` run driven by `fake_model`. |

---

## Behavior (BDD) — Gherkin

Full content of `tests/services/orchestrator/features/tool_loop_detection.feature`:

```gherkin
Feature: Tool-loop / no-progress detection in the ReAct executor
  On a weak local model the executor sometimes repeats the same tool call
  with the same arguments and burns every step. The loop detector spots a
  consecutive repeat or a short cycle of signatures and breaks the loop early
  with an honest failure, while never tripping on legitimately distinct calls.

  Background:
    Given a loop detector with the default repeat limit

  @mocked
  Scenario: A consecutive repeat of the same call trips the break
    When the call "run_bash" with arguments {"command": "ls"} is recorded
    And the call "run_bash" with arguments {"command": "ls"} is recorded
    Then the detector reports it should break
    And the trip reason mentions "repeat"

  @mocked
  Scenario: Argument key order does not matter for repeat detection
    When the call "call_skill_tool" with arguments {"skill": "x", "tool": "y"} is recorded
    And the call "call_skill_tool" with arguments {"tool": "y", "skill": "x"} is recorded
    Then the detector reports it should break

  @mocked
  Scenario: Distinct calls do not trip the break
    When the call "read_file" with arguments {"path": "a.txt"} is recorded
    And the call "read_file" with arguments {"path": "b.txt"} is recorded
    And the call "read_file" with arguments {"path": "c.txt"} is recorded
    Then the detector reports it should not break

  @mocked
  Scenario: A two-signature cycle with no new signature trips the break
    When the call "run_bash" with arguments {"command": "ls"} is recorded
    And the call "run_bash" with arguments {"command": "pwd"} is recorded
    And the call "run_bash" with arguments {"command": "ls"} is recorded
    And the call "run_bash" with arguments {"command": "pwd"} is recorded
    Then the detector reports it should break
    And the trip reason mentions "cycle"

  @mocked
  Scenario: A new signature after a near-cycle resets progress and does not trip
    When the call "run_bash" with arguments {"command": "ls"} is recorded
    And the call "run_bash" with arguments {"command": "pwd"} is recorded
    And the call "run_bash" with arguments {"command": "ls"} is recorded
    And the call "run_bash" with arguments {"command": "whoami"} is recorded
    Then the detector reports it should not break

  @mocked
  Scenario Outline: The repeat threshold is configurable
    Given a loop detector with repeat limit <limit>
    When the call "run_bash" with arguments {"command": "ls"} is recorded <count> times
    Then the detector should_break is <result>

    Examples:
      | limit | count | result |
      | 2     | 1     | False  |
      | 2     | 2     | True   |
      | 3     | 2     | False  |
      | 3     | 3     | True   |

  @mocked
  Scenario: The ReAct loop breaks early when the model repeats one tool call
    Given a ReAct orchestrator wired to a fake model that always calls run_bash with the same arguments
    When the goal "loop forever" is executed
    Then react_execute returns ok False
    And the summary mentions a loop
    And the model was called fewer times than max_steps
```

---

## Task 1: Pure `LoopDetector` module

**Files:**
- Create: `services/orchestrator/loop_detection.py`
- Test: `tests/services/orchestrator/test_loop_detection.py`

**Interfaces:**
- Consumes: nothing (pure module; stdlib `json`, `os`, `logging` only).
- Produces, relied on by Task 3 (wire-in) and Task 2 (BDD steps):
  - `LOOP_REPEAT_LIMIT: int` — module constant, `int(os.getenv("LOOP_REPEAT_LIMIT", "2"))`.
  - `call_signature(name: str, args: dict) -> str` — deterministic `"<name>::<json.dumps(args, sort_keys=True, default=str)>"`.
  - `class LoopDetector:`
    - `__init__(self, repeat_limit: int | None = None, cycle_window: int = 6) -> None`
    - `record(self, signature: str) -> bool` — append the signature, return current `should_break`.
    - `should_break(self) -> bool` — True if the last `repeat_limit` signatures are identical, OR the recent window cycles a small set of signatures with no signature seen for the first time in the last `repeat_limit` steps.
    - `reason(self) -> str` — `""`, `"repeat"`, or `"cycle"`; the cause of the most recent trip.
    - `reset(self) -> None` — clear history (per-goal boundary).

- [ ] **Step 1: Write the failing test**

Create `tests/services/orchestrator/test_loop_detection.py`:

```python
# tests/services/orchestrator/test_loop_detection.py
from __future__ import annotations

import pytest

from services.orchestrator.loop_detection import (
    LoopDetector,
    call_signature,
    LOOP_REPEAT_LIMIT,
)


@pytest.mark.mocked
class TestCallSignature:
    def test_signature_is_deterministic_regardless_of_key_order(self):
        a = call_signature("call_skill_tool", {"skill": "x", "tool": "y"})
        b = call_signature("call_skill_tool", {"tool": "y", "skill": "x"})
        assert a == b

    def test_signature_includes_tool_name(self):
        sig = call_signature("run_bash", {"command": "ls"})
        assert sig.startswith("run_bash")

    def test_different_args_produce_different_signatures(self):
        a = call_signature("read_file", {"path": "a.txt"})
        b = call_signature("read_file", {"path": "b.txt"})
        assert a != b

    def test_non_serializable_args_do_not_raise(self):
        # default=str must keep this from blowing up
        sig = call_signature("run_bash", {"obj": object()})
        assert sig.startswith("run_bash")


@pytest.mark.mocked
class TestLoopDetectorRepeat:
    def test_single_call_does_not_break(self):
        d = LoopDetector(repeat_limit=2)
        assert d.record(call_signature("run_bash", {"command": "ls"})) is False
        assert d.should_break() is False

    def test_consecutive_repeat_trips_at_limit(self):
        d = LoopDetector(repeat_limit=2)
        d.record(call_signature("run_bash", {"command": "ls"}))
        tripped = d.record(call_signature("run_bash", {"command": "ls"}))
        assert tripped is True
        assert d.should_break() is True
        assert d.reason() == "repeat"

    def test_distinct_calls_never_trip(self):
        d = LoopDetector(repeat_limit=2)
        d.record(call_signature("read_file", {"path": "a.txt"}))
        d.record(call_signature("read_file", {"path": "b.txt"}))
        assert d.record(call_signature("read_file", {"path": "c.txt"})) is False
        assert d.should_break() is False

    def test_repeat_limit_three_needs_three(self):
        d = LoopDetector(repeat_limit=3)
        d.record(call_signature("run_bash", {"command": "ls"}))
        assert d.record(call_signature("run_bash", {"command": "ls"})) is False
        assert d.record(call_signature("run_bash", {"command": "ls"})) is True
        assert d.reason() == "repeat"

    def test_default_limit_from_env_constant(self):
        # LOOP_REPEAT_LIMIT defaults to 2 unless overridden in the environment.
        assert LOOP_REPEAT_LIMIT >= 1
        d = LoopDetector()  # uses the module default
        d.record(call_signature("run_bash", {"command": "ls"}))
        # With default 2 a single repeat trips; tolerate higher env overrides.
        for _ in range(LOOP_REPEAT_LIMIT - 1):
            d.record(call_signature("run_bash", {"command": "ls"}))
        assert d.should_break() is True


@pytest.mark.mocked
class TestLoopDetectorCycle:
    def test_two_signature_cycle_trips(self):
        d = LoopDetector(repeat_limit=2)
        for cmd in ["ls", "pwd", "ls", "pwd"]:
            tripped = d.record(call_signature("run_bash", {"command": cmd}))
        assert tripped is True
        assert d.should_break() is True
        assert d.reason() == "cycle"

    def test_new_signature_resets_progress(self):
        d = LoopDetector(repeat_limit=2)
        d.record(call_signature("run_bash", {"command": "ls"}))
        d.record(call_signature("run_bash", {"command": "pwd"}))
        d.record(call_signature("run_bash", {"command": "ls"}))
        # A genuinely new command means progress — must NOT trip.
        assert d.record(call_signature("run_bash", {"command": "whoami"})) is False
        assert d.should_break() is False

    def test_reset_clears_history(self):
        d = LoopDetector(repeat_limit=2)
        d.record(call_signature("run_bash", {"command": "ls"}))
        d.record(call_signature("run_bash", {"command": "ls"}))
        assert d.should_break() is True
        d.reset()
        assert d.should_break() is False
        assert d.reason() == ""
        assert d.record(call_signature("run_bash", {"command": "ls"})) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/services/orchestrator/test_loop_detection.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.orchestrator.loop_detection'`

- [ ] **Step 3: Write minimal implementation**

Create `services/orchestrator/loop_detection.py`:

```python
# services/orchestrator/loop_detection.py
"""
Pure, dependency-free tool-loop / no-progress detector for the ReAct executor.

A weak local Q4 model often repeats the same tool call with the same arguments,
or cycles a small set of calls, and burns every step without making progress.
This module turns each (tool_name, normalized_arguments) pair into a
deterministic signature and reports when the executor should break early.

Design rules (see the implementation plan's Global Constraints):
  - No LLM, no async, no Redis, no imports from the orchestrator. Pure stdlib.
  - Deterministic normalization: json.dumps(args, sort_keys=True, default=str).
  - Conservative: trips only on an immediate consecutive repeat OR a short cycle
    with no new signature; legitimately distinct calls never trip it.
  - stderr logging only (never stdout — MCP rule).
"""
from __future__ import annotations

import json
import logging
import os

_log = logging.getLogger("loop_detection")

# Number of consecutive identical signatures that counts as a stuck loop.
# Matches the env-knob style used in graph.py (module-level getenv + cast).
LOOP_REPEAT_LIMIT = int(os.getenv("LOOP_REPEAT_LIMIT", "2"))


def call_signature(name: str, args: dict) -> str:
    """Deterministic signature for a tool call.

    Argument key order must never change the signature, so we sort keys. Values
    that are not JSON-serializable fall back to str() rather than raising — the
    detector must never crash the executor.
    """
    try:
        norm = json.dumps(args or {}, sort_keys=True, default=str)
    except Exception:
        norm = str(args)
    return f"{name}::{norm}"


class LoopDetector:
    """Ingests call signatures and reports whether the loop should break.

    `record(sig)` appends a signature and returns the current should_break().
    `should_break()` is True when EITHER:
      - the last `repeat_limit` signatures are all identical (a repeat), OR
      - within the recent `cycle_window` no signature has been seen for the
        FIRST time in the last `repeat_limit` steps AND the window cycles a
        small set (<= repeat_limit distinct signatures) — a no-progress cycle.
    """

    def __init__(self, repeat_limit: int | None = None, cycle_window: int = 6) -> None:
        self.repeat_limit = LOOP_REPEAT_LIMIT if repeat_limit is None else repeat_limit
        self.cycle_window = cycle_window
        self._sigs: list[str] = []
        self._reason = ""

    def reset(self) -> None:
        """Clear history — call at the per-goal boundary."""
        self._sigs = []
        self._reason = ""

    def record(self, signature: str) -> bool:
        self._sigs.append(signature)
        return self.should_break()

    def reason(self) -> str:
        return self._reason

    def should_break(self) -> bool:
        if self.repeat_limit < 1:
            return False
        sigs = self._sigs
        n = len(sigs)
        if n < self.repeat_limit:
            return False

        # 1) Immediate consecutive repeat.
        tail = sigs[-self.repeat_limit:]
        if len(set(tail)) == 1:
            self._reason = "repeat"
            return True

        # 2) No-progress cycle. Look at the recent window; if it cycles a small
        # set of signatures and nothing in the last `repeat_limit` steps is a
        # FIRST-time signature (i.e. no genuine progress), treat it as a loop.
        window = sigs[-self.cycle_window:]
        if len(window) >= 2 * self.repeat_limit:
            distinct = set(window)
            # Small alternating/cycling set: distinct count is at most repeat_limit
            # and every recent step was already seen earlier in the window.
            if len(distinct) <= self.repeat_limit:
                recent = sigs[-self.repeat_limit:]
                earlier = set(sigs[:-self.repeat_limit])
                if all(s in earlier for s in recent):
                    self._reason = "cycle"
                    return True

        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/services/orchestrator/test_loop_detection.py -v`
Expected: PASS (all tests green)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/loop_detection.py tests/services/orchestrator/test_loop_detection.py
git commit -m "feat(orchestrator): pure LoopDetector for tool-loop/no-progress detection

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: pytest-bdd step definitions for the detector behavior

**Files:**
- Create: `tests/services/orchestrator/features/tool_loop_detection.feature` (content in the Behavior section above)
- Create: `tests/services/orchestrator/test_tool_loop_detection_bdd.py`

**Interfaces:**
- Consumes: `LoopDetector`, `call_signature`, `LOOP_REPEAT_LIMIT` from Task 1; `AsyncOrchestrator.react_execute` wired in Task 3; the `fake_model` respx fixture from the foundation `tests/conftest.py`.
- Produces: nothing imported elsewhere — this is the executable behavior contract.

- [ ] **Step 1: Create the feature file**

Create `tests/services/orchestrator/features/tool_loop_detection.feature` with the EXACT content shown in the "Behavior (BDD) — Gherkin" section above. (Copy it verbatim — do not paraphrase.)

- [ ] **Step 2: Write the failing step-definition test**

Create `tests/services/orchestrator/test_tool_loop_detection_bdd.py`:

```python
# tests/services/orchestrator/test_tool_loop_detection_bdd.py
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.loop_detection import LoopDetector, call_signature

scenarios("features/tool_loop_detection.feature")


# ── Shared mutable context for the detector scenarios ──────────────────────
@pytest.fixture
def ctx():
    return {"detector": None, "last_break": False, "model_calls": 0, "react_result": None}


@given("a loop detector with the default repeat limit")
def _default_detector(ctx):
    ctx["detector"] = LoopDetector()


@given(parsers.parse("a loop detector with repeat limit {limit:d}"))
def _detector_with_limit(ctx, limit):
    ctx["detector"] = LoopDetector(repeat_limit=limit)


@when(parsers.parse('the call "{name}" with arguments {args} is recorded'))
def _record_call(ctx, name, args):
    sig = call_signature(name, json.loads(args))
    ctx["last_break"] = ctx["detector"].record(sig)


@when(parsers.parse('the call "{name}" with arguments {args} is recorded {count:d} times'))
def _record_call_n(ctx, name, args, count):
    sig = call_signature(name, json.loads(args))
    for _ in range(count):
        ctx["last_break"] = ctx["detector"].record(sig)


@then("the detector reports it should break")
def _should_break(ctx):
    assert ctx["detector"].should_break() is True


@then("the detector reports it should not break")
def _should_not_break(ctx):
    assert ctx["detector"].should_break() is False


@then(parsers.parse('the trip reason mentions "{word}"'))
def _reason_mentions(ctx, word):
    assert word in ctx["detector"].reason()


@then(parsers.parse("the detector should_break is {result}"))
def _should_break_is(ctx, result):
    expected = result.strip() == "True"
    assert ctx["detector"].should_break() is expected


# ── Wire-in scenario: a fake model that always repeats one tool call ───────
def _repeat_tool_response():
    """A litellm-shaped response whose message always calls run_bash {command: ls}."""
    tc = MagicMock()
    tc.id = "call_loop"
    tc.function = MagicMock()
    tc.function.name = "run_bash"
    tc.function.arguments = json.dumps({"command": "ls"})
    msg = MagicMock()
    msg.content = None
    msg.tool_calls = [tc]
    msg.reasoning_content = ""
    msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    return MagicMock(choices=[MagicMock(message=msg)])


@given("a ReAct orchestrator wired to a fake model that always calls run_bash with the same arguments")
def _orch_with_looping_model(ctx):
    from services.orchestrator.coding_orchestrator import AsyncOrchestrator

    orch = AsyncOrchestrator(skill_router=None, mcp=AsyncMock(), workspace="/tmp", max_steps=6)
    bash_result = MagicMock()
    bash_result.content = [MagicMock(text="files")]
    bash_result.isError = False
    orch.mcp.call_tool = AsyncMock(return_value=bash_result)
    ctx["orch"] = orch


@when(parsers.parse('the goal "{goal}" is executed'))
def _execute_goal(ctx, goal):
    orch = ctx["orch"]

    async def _counting(*a, **k):
        ctx["model_calls"] += 1
        return _repeat_tool_response()

    async def _run():
        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, side_effect=_counting):
            return await orch.react_execute(goal)

    import asyncio
    ctx["react_result"] = asyncio.get_event_loop().run_until_complete(_run())


@then("react_execute returns ok False")
def _react_ok_false(ctx):
    assert ctx["react_result"]["ok"] is False


@then("the summary mentions a loop")
def _summary_mentions_loop(ctx):
    assert "loop" in ctx["react_result"]["summary"].lower()


@then("the model was called fewer times than max_steps")
def _fewer_than_max_steps(ctx):
    assert ctx["model_calls"] < ctx["orch"].max_steps
```

> Note: `scenarios("features/tool_loop_detection.feature")` resolves relative to this test file's directory. The pytest-bdd plugin and the `fake_model` respx fixture are provided by the foundation `tests/conftest.py` (assumed to exist per Global Constraints).

- [ ] **Step 3: Run the BDD detector scenarios to verify they fail on the wire-in scenario only**

Run: `python -m pytest tests/services/orchestrator/test_tool_loop_detection_bdd.py -v`
Expected: the pure-detector scenarios PASS (Task 1 is done); the final scenario ("The ReAct loop breaks early…") FAILS because the wire-in (Task 3) is not done yet — it will hit `max_steps` and `react_execute` returns `{"ok": False, "summary": "max_steps reached"}`, so `_summary_mentions_loop` and `_fewer_than_max_steps` fail.

- [ ] **Step 4: (No code in this task)**

The wire-in that makes the final scenario pass is Task 3. Confirm only the last scenario is red.

Run: `python -m pytest tests/services/orchestrator/test_tool_loop_detection_bdd.py -v -k "not breaks_early and not break early"`
Expected: PASS (all pure-detector scenarios green)

- [ ] **Step 5: Commit**

```bash
git add tests/services/orchestrator/features/tool_loop_detection.feature tests/services/orchestrator/test_tool_loop_detection_bdd.py
git commit -m "test(orchestrator): pytest-bdd contract for tool-loop detection

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Wire the detector into the ReAct loop

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py` (`react_execute`: the import block ~line 191, the per-goal reset ~lines 196–202, and inside the `for step in range(self.max_steps)` tool-dispatch loop ~lines 472–608)
- Test: `tests/services/orchestrator/test_coding_orchestrator.py` (add to existing `TestReactExecute` class)

**Interfaces:**
- Consumes: `LoopDetector`, `call_signature` from Task 1; `events.emit` from `services/orchestrator/events.py`.
- Produces: a new best-effort event `loop.detected` (fields: `tool`, `reason`, `signature`, `steps`); `react_execute` now returns `{"ok": False, "summary": "loop detected (<reason>): repeated tool '<name>' — halting to avoid burning steps"}` when the detector trips.

- [ ] **Step 1: Write the failing test**

Add to `tests/services/orchestrator/test_coding_orchestrator.py` inside the `TestReactExecute` class (after `test_react_execute_max_steps_exhausted`):

```python
    @pytest.mark.asyncio
    async def test_react_execute_breaks_on_tool_loop_before_max_steps(self):
        """Detector trips on a repeated run_bash call and halts before max_steps."""
        orch = self._make_orch(max_steps=6)

        # Model ALWAYS calls run_bash {command: ls} — a no-progress loop.
        def _looping_response():
            return self._make_tool_call_response("run_bash", {"command": "ls"})

        call_count = {"n": 0}

        async def _counting(*a, **k):
            call_count["n"] += 1
            return _looping_response()

        mcp = AsyncMock()
        mcp_result = MagicMock()
        mcp_result.content = [MagicMock(text="files")]
        mcp_result.isError = False
        mcp.call_tool.return_value = mcp_result
        orch.mcp = mcp

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, side_effect=_counting):
            result = await orch.react_execute("loop forever")

        assert result["ok"] is False
        assert "loop" in result["summary"].lower()
        # Must halt before exhausting all 6 steps.
        assert call_count["n"] < 6

    @pytest.mark.asyncio
    async def test_react_execute_distinct_calls_do_not_trip_loop(self):
        """Distinct read_file paths must NOT trip the detector; finish ends cleanly."""
        orch = self._make_orch(max_steps=6, mcp=None)
        orch.redis = None  # read_file without redis returns a structured error, not a crash

        # Three distinct reads, then finish — no two consecutive identical sigs.
        r1 = self._make_tool_call_response("read_file", {"path": "a.txt"})
        r2 = self._make_tool_call_response("read_file", {"path": "b.txt"})
        r3 = self._make_tool_call_response("read_file", {"path": "c.txt"})
        rf = MagicMock()
        mf = MagicMock()
        mf.content = None
        tcf = MagicMock()
        tcf.id = "call_fin"
        tcf.function.name = "finish"
        tcf.function.arguments = json.dumps({"summary": "all read"})
        mf.tool_calls = [tcf]
        rf.choices = [MagicMock(message=mf)]

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, side_effect=[r1, r2, r3, rf]):
            result = await orch.react_execute("read three files")

        assert result["ok"] is True
        assert result["summary"] == "all read"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/services/orchestrator/test_coding_orchestrator.py::TestReactExecute::test_react_execute_breaks_on_tool_loop_before_max_steps -v`
Expected: FAIL — currently the loop runs all 6 steps and returns `{"ok": False, "summary": "max_steps reached"}`, so `"loop" in summary` and `call_count < 6` both fail.

- [ ] **Step 3a: Add the import**

In `services/orchestrator/coding_orchestrator.py`, add the import next to the existing local imports near the top of the module (after line 15, `from .local_tools import ...`):

```python
from .loop_detection import LoopDetector, call_signature
```

- [ ] **Step 3b: Instantiate the detector per goal**

In `react_execute`, just after the per-goal `reset_activations()` block (after line ~202, before the "Skill-first deterministic routing" comment), add:

```python
        # Per-goal tool-loop detector. A weak model can repeat the same tool call
        # or cycle a tiny set of calls and burn every step. Detect and halt early.
        loop_detector = LoopDetector()
```

- [ ] **Step 3c: Feed each dispatched tool call and break on trip**

Inside the `for tc in tool_calls:` loop, after `args` is parsed and the `finish` early-return, but BEFORE the `tool.start` emit (between line ~485 `}` of the finish branch and line ~487 `# Emit tool.start`), insert the detector check:

```python
                    # No-progress / tool-loop detection. finish already returned
                    # above, so only genuinely dispatched tools reach here.
                    if loop_detector.record(call_signature(name, args)):
                        _reason = loop_detector.reason()
                        await events.emit(
                            "loop.detected",
                            tool=name,
                            reason=_reason,
                            signature=call_signature(name, args),
                            steps=step + 1,
                        )
                        import logging as _logging
                        _logging.getLogger("orchestrator").warning(
                            "tool-loop detected (%s) on '%s' at step %d — halting",
                            _reason, name, step + 1,
                        )
                        return {
                            "ok": False,
                            "summary": (
                                f"loop detected ({_reason}): repeated tool "
                                f"'{name}' — halting to avoid burning steps"
                            ),
                        }
```

This sits at the same indentation as the `if name == "finish":` block (12 spaces / inside `for tc in tool_calls:`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/services/orchestrator/test_coding_orchestrator.py::TestReactExecute::test_react_execute_breaks_on_tool_loop_before_max_steps tests/services/orchestrator/test_coding_orchestrator.py::TestReactExecute::test_react_execute_distinct_calls_do_not_trip_loop -v`
Expected: PASS (both)

- [ ] **Step 5: Run the full existing orchestrator suite for regression safety**

Run: `python -m pytest tests/services/orchestrator/test_coding_orchestrator.py -v`
Expected: PASS — all pre-existing tests still green (notably `test_react_execute_max_steps_exhausted`, which uses two *different* commands `ls`/`pwd` only twice with `max_steps=2`, so the detector does NOT trip and the `max_steps` path is preserved).

- [ ] **Step 6: Run the BDD contract to confirm the wire-in scenario now passes**

Run: `python -m pytest tests/services/orchestrator/test_tool_loop_detection_bdd.py -v`
Expected: PASS — including "The ReAct loop breaks early when the model repeats one tool call".

- [ ] **Step 7: Commit**

```bash
git add services/orchestrator/coding_orchestrator.py tests/services/orchestrator/test_coding_orchestrator.py
git commit -m "feat(orchestrator): break ReAct loop on repeated/cycling tool calls

Wires LoopDetector into react_execute: each dispatched tool call feeds a
deterministic (name, sorted-args) signature; a consecutive repeat or a short
no-progress cycle halts the loop early with an honest failure and a
loop.detected event, instead of burning all max_steps. finish is never
counted; distinct calls never trip. Threshold via LOOP_REPEAT_LIMIT (default 2).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage**

| Spec requirement | Task |
|---|---|
| Track a signature of each `(tool_name, normalized_arguments)` call | Task 1 — `call_signature()` |
| Same signature repeats N times consecutively → break | Task 1 `should_break` repeat branch; Task 3 wire-in; BDD scenario 1 |
| Small set of signatures cycles with no new tool/finish → break | Task 1 cycle branch; BDD "two-signature cycle" scenario |
| Conservative — don't trip on legitimately different reads | Task 1 `test_distinct_calls_never_trip`; Task 3 `test_react_execute_distinct_calls_do_not_trip_loop`; BDD "Distinct calls" + "new signature resets" |
| Threshold configurable via env `LOOP_REPEAT_LIMIT` default 2 | Task 1 `LOOP_REPEAT_LIMIT`; BDD Scenario Outline |
| Emit event/log on trip | Task 3 `loop.detected` emit + stderr warning |
| Detector in new module `loop_detection.py`, pure & unit-testable | Task 1 module + `test_loop_detection.py` |
| Deterministic normalization `json.dumps(..., sort_keys=True)` | Task 1 `call_signature`; BDD "Argument key order does not matter" |
| Pure (no LLM/async needed for core logic) | Task 1 — sync class, stdlib only |
| Wire-in regression-safe when no repetition | Task 3 Step 5 (full suite) + `test_react_execute_max_steps_exhausted` preserved |
| BDD feature file + step defs + unit TDD at the contracted paths | Tasks 1 & 2 at the exact paths in the File Map |

No gaps.

**2. Placeholder scan**

No "TBD"/"TODO"/"add error handling"/"similar to". Every code step contains complete, runnable code. The only forward reference is Task 2's wire-in scenario, which is explicitly documented as failing until Task 3 — that is a TDD red state, not a placeholder.

**3. Type consistency**

- `call_signature(name, args) -> str` and `LoopDetector(repeat_limit=..., cycle_window=...)` with methods `record`/`should_break`/`reason`/`reset` are used identically across `test_loop_detection.py`, `test_tool_loop_detection_bdd.py`, and the `coding_orchestrator.py` wire-in.
- `LOOP_REPEAT_LIMIT` is imported and referenced consistently in Task 1's tests and the detector default.
- The wire-in's early return uses the same `{"ok": bool, "summary": str}` shape as every other `react_execute` return path (verified against the real source: lines 264, 468, 482, 611, 614).
- `events.emit("loop.detected", ...)` matches the variadic `emit(type, **fields)` signature in `events.py`.
- `step + 1` for the `steps` field is valid because the detector check lives inside `for step in range(self.max_steps)`.

**4. State/config additivity**

No `State` field is added — the detector is loop-local. `LOOP_REPEAT_LIMIT` is a new module-level constant only. `types.py` is untouched. Additive-only constraint satisfied.

Plan complete and saved to `docs/superpowers/plans/2026-06-25-tool-loop-detection.md`.
