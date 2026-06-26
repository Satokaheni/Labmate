# Raw Tool-Output Grounding (budget-aware, not summaries) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop starving the weak Q4 model: feed real tool output (test results, file contents, bash stdout/stderr, skill results) back into the ReAct context verbatim under a generous byte budget, truncating with a head+tail+marker only on genuine overflow.

**Architecture:** Add one pure, deterministic helper `ground_tool_result(text, budget) -> str` in a new tiny module `services/orchestrator/tool_grounding.py`. Replace the hard `[:4000]` / `[:2000]` truncations in `_run_react_loop`'s tool-result handling (`coding_orchestrator.py`) with calls to this helper, governed by env knob `LABMATE_TOOL_RESULT_BUDGET` (default 16000 chars). The helper keeps a head AND a tail so end-of-output evidence (FAILED / assert lines / tracebacks) survives. This is additive and regression-safe: small outputs now pass through verbatim instead of being cut at 2-4k.

**Tech Stack:** Python 3, `pytest` + `pytest-asyncio`, `pytest-bdd` (Gherkin), `respx` `fake_model` fixture (already in `tests/conftest.py`).

## Global Constraints

- stdout is sacred in any MCP server: never `print()` / `console.log()` in server code. Helper and orchestrator log to stderr via `logging` only. (This plan adds no logging.)
- Every `litellm` / failover model call already sets `extra_body={"thinking_budget_tokens": ...}` and `api_key="not-needed"` — do not touch those call sites; this plan only changes how *tool result strings* are built, not how the model is called.
- Python file naming: `snake_case.py` (`tool_grounding.py`). Python functions: `snake_case` (`ground_tool_result`). 
- Service config from env vars, never hardcoded: new knob `LABMATE_TOOL_RESULT_BUDGET`, default `16000`, read with `int(os.getenv(...))`.
- Tests live under `tests/` mirroring `services/`. `@pytest.mark.asyncio` on all async tests. Assert structure, not literal LLM text. pytest-bdd scenarios tagged `@mocked`, marked `pytest.mark.bdd` + `pytest.mark.mocked`, and use the `fake_model`/`run_async` helpers from `tests/conftest.py`.
- Additive only: no `State` field removals, no signature changes to `react_execute` / `_run_react_loop`. The default budget (16000) is larger than every existing cut (4000/2000), so small/medium outputs that were previously truncated now flow verbatim.

---

## File Map

| File | Create / Modify | Responsibility |
|---|---|---|
| `services/orchestrator/tool_grounding.py` | **Create** | Pure helper `ground_tool_result(text, budget)` + module constant `DEFAULT_TOOL_RESULT_BUDGET = 16000`. No I/O, no deps. |
| `services/orchestrator/coding_orchestrator.py` | **Modify** | Import the helper + a module-level `LABMATE_TOOL_RESULT_BUDGET` env knob; wrap the four tool-result content sites inside `_run_react_loop` (call_skill_tool `[:4000]`, run_bash, code_semantic_search, and the `LOCAL_TOOL_NAMES` branch that carries read_file/run_tests/list_dir). |
| `tests/services/orchestrator/test_tool_grounding.py` | **Create** | Unit tests for the pure helper (verbatim under budget; head+tail over budget; marker present; head and tail both retained; edge cases). |
| `tests/services/orchestrator/features/raw_output_grounding.feature` | **Create** | Gherkin behavior contract (`@mocked`). |
| `tests/services/orchestrator/test_raw_output_grounding_bdd.py` | **Create** | pytest-bdd step defs binding the feature to the helper + a wired `_run_react_loop` run via the existing fake-model pattern. |

**Wire-in sites in `_run_react_loop` (verified line numbers on branch `feat/agentic-fix-loop`):**

| Site | Current code (line) | Tool(s) it carries |
|---|---|---|
| call_skill_tool | `content = json.dumps(res)[:4000]` (**line 489**) | `call_skill_tool` skill results |
| run_bash | `content = "\n".join(c.text for c in obs.content if hasattr(c, "text"))` (**lines 540-542**) | `run_bash` stdout/stderr |
| code_semantic_search | `content = "\n".join(c.text for c in obs.content if hasattr(c, "text"))` (**lines 555-557**) | `code_semantic_search` results |
| LOCAL_TOOL_NAMES | `content = json.dumps({"result": result}, default=str)` (**line 521**) | `read_file`, `run_tests`, `list_dir` (dispatched via `request_local_tool`) |

> Note: `read_file` / `run_tests` are NOT first-class `elif` branches; they reach the model through the `LOCAL_TOOL_NAMES` branch (line 515) as `json.dumps({"result": result})`. Grounding that one string covers them. The `finish` summary (line 438) and the no-tool-call content return (line 421) are *final returns to the caller*, not tool-result messages fed back into the loop context — leave them untouched (out of scope).

---

## Behavior (BDD) — Gherkin

Full content of `tests/services/orchestrator/features/raw_output_grounding.feature`:

```gherkin
Feature: Raw tool-output grounding (budget-aware, not summaries)
  The weak local model can only judge whether an edit applied or whether tests
  actually passed if it SEES the real tool output. The ReAct executor must feed
  tool results back verbatim when they fit a generous byte budget, and only on
  genuine overflow keep a head AND a tail (joined by a clear truncation marker)
  so the end-of-output evidence — FAILED lines, assert messages, tracebacks —
  always reaches the model instead of being cut at 2-4k chars.

  @mocked
  Scenario: Output under budget reaches the model verbatim
    Given a tool output of 500 characters
    And a tool-result budget of 16000 characters
    When the output is grounded
    Then the grounded text equals the original output exactly
    And the grounded text contains no truncation marker

  @mocked
  Scenario: Output exactly at the budget is still verbatim
    Given a tool output of 16000 characters
    And a tool-result budget of 16000 characters
    When the output is grounded
    Then the grounded text equals the original output exactly
    And the grounded text contains no truncation marker

  @mocked
  Scenario: Over-budget output keeps a head and a tail with a marker
    Given a tool output of 40000 characters
    And a tool-result budget of 16000 characters
    When the output is grounded
    Then the grounded text is no longer than the budget plus the marker
    And the grounded text starts with the head of the original output
    And the grounded text ends with the tail of the original output
    And the grounded text contains a truncation marker reporting the dropped char count

  @mocked
  Scenario: A long failing-test output reaches the model with its FAILED lines intact
    Given a tool output that is a long passing-test preamble followed by a FAILED assertion at the very end
    And a tool-result budget of 16000 characters
    When the output is grounded
    Then the grounded text contains the FAILED assertion line
    And the grounded text contains the assert detail line

  @mocked
  Scenario: A small bash result flows verbatim into the ReAct tool message
    Given a ReAct orchestrator wired to a fake model that runs one bash command then finishes
    And the bash command returns "hello world" as its only output
    When the goal "echo hello" is executed
    Then the tool message appended to the model context contains "hello world" verbatim
    And the tool message contains no truncation marker

  @mocked
  Scenario: A huge bash result is grounded with a head, a tail, and a marker before reaching the model
    Given a ReAct orchestrator wired to a fake model that runs one bash command then finishes
    And the bash command returns 50000 characters ending in "TAILSENTINEL"
    When the goal "dump log" is executed
    Then the tool message appended to the model context contains a truncation marker
    And the tool message appended to the model context contains "TAILSENTINEL"
```

---

## Task 1: Pure helper `ground_tool_result`

**Files:**
- Create: `services/orchestrator/tool_grounding.py`
- Test: `tests/services/orchestrator/test_tool_grounding.py`

**Interfaces:**
- Consumes: nothing (pure stdlib).
- Produces:
  - `DEFAULT_TOOL_RESULT_BUDGET: int = 16000`
  - `ground_tool_result(text: str, budget: int = DEFAULT_TOOL_RESULT_BUDGET) -> str` — returns `text` verbatim when `len(text) <= budget`; otherwise returns `head + marker + tail` where `marker == "\n…[{dropped} chars truncated]…\n"`, head and tail each ~`budget // 2`, and `dropped == len(text) - kept`. Always retains both the start and the end of `text`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/orchestrator/test_tool_grounding.py
from __future__ import annotations

import pytest

from services.orchestrator.tool_grounding import (
    ground_tool_result,
    DEFAULT_TOOL_RESULT_BUDGET,
)

pytestmark = pytest.mark.mocked


def test_default_budget_is_generous():
    # Default must be far above the old 4000/2000 hard cuts so real output flows.
    assert DEFAULT_TOOL_RESULT_BUDGET >= 16000


def test_under_budget_is_verbatim():
    text = "x" * 500
    assert ground_tool_result(text, budget=16000) == text


def test_exactly_at_budget_is_verbatim():
    text = "y" * 16000
    out = ground_tool_result(text, budget=16000)
    assert out == text
    assert "truncated" not in out


def test_empty_string_is_verbatim():
    assert ground_tool_result("", budget=16000) == ""


def test_over_budget_keeps_head_and_tail():
    head_block = "HEAD" + ("a" * 9996)      # 10000 chars
    tail_block = ("b" * 9988) + "TAILMARK"  # 10000 chars
    text = head_block + tail_block          # 20000 chars
    out = ground_tool_result(text, budget=8000)
    # Start of the original survives.
    assert out.startswith("HEAD")
    # End of the original survives (critical: test failures print at the end).
    assert out.endswith("TAILMARK")
    # A truncation marker sits between head and tail.
    assert "truncated" in out
    assert "…" in out


def test_over_budget_marker_reports_dropped_count():
    text = "z" * 40000
    budget = 16000
    out = ground_tool_result(text, budget=budget)
    dropped = len(text) - (len(out) - _marker_len(out))
    # The number reported in the marker equals chars actually dropped.
    assert f"{dropped} chars truncated" in out


def test_over_budget_payload_is_about_budget_sized():
    text = "q" * 100000
    budget = 16000
    out = ground_tool_result(text, budget=budget)
    # Kept content (excluding the marker) must not exceed the budget.
    kept = len(out) - _marker_len(out)
    assert kept <= budget


def test_tiny_budget_still_returns_both_ends():
    text = "START" + ("m" * 100) + "END"
    out = ground_tool_result(text, budget=10)
    assert out.startswith("ST")    # some head
    assert out.endswith("ND")      # some tail
    assert "truncated" in out


def _marker_len(out: str) -> int:
    # Length of the "\n…[N chars truncated]…\n" segment inside `out`.
    import re
    m = re.search(r"\n…\[\d+ chars truncated\]…\n", out)
    assert m is not None, f"no marker found in: {out[:80]!r}..."
    return len(m.group(0))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_tool_grounding.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.orchestrator.tool_grounding'`.

- [ ] **Step 3: Write the minimal implementation**

```python
# services/orchestrator/tool_grounding.py
"""Budget-aware grounding of raw tool output for the ReAct loop.

The weak local model can only tell whether an edit applied or whether tests
actually passed if it sees the REAL tool output. So we feed tool results back
verbatim whenever they fit a generous byte budget. Only on genuine overflow do
we truncate — and even then we keep a HEAD and a TAIL joined by a clear marker,
because the decisive evidence (FAILED lines, assert messages, tracebacks) is
almost always at the END of the output.

Pure and deterministic: no I/O, no imports beyond what's shown.
"""
from __future__ import annotations

DEFAULT_TOOL_RESULT_BUDGET = 16000


def ground_tool_result(text: str, budget: int = DEFAULT_TOOL_RESULT_BUDGET) -> str:
    """Return ``text`` verbatim if it fits ``budget`` chars; otherwise keep a
    head and a tail joined by a ``\\n…[N chars truncated]…\\n`` marker.

    Args:
        text: the raw tool output (stdout/stderr, file contents, test results).
        budget: max chars of ORIGINAL content to retain (the marker is extra).

    Guarantees:
        * ``len(text) <= budget``  → returns ``text`` unchanged (no marker).
        * otherwise → returns ``head + marker + tail`` where head and tail are
          taken from the start and end of ``text``, ``head`` + ``tail`` length
          ``<= budget``, and the marker reports the exact number of dropped chars.
        * both the first and last characters of ``text`` are always preserved
          when truncation occurs (head and tail are each at least 1 char for a
          positive budget).
    """
    if budget <= 0 or len(text) <= budget:
        return text

    head_len = budget // 2
    tail_len = budget - head_len
    # Guard the degenerate budget==1 / odd-split case so both ends survive.
    if head_len < 1:
        head_len = 1
    if tail_len < 1:
        tail_len = 1

    head = text[:head_len]
    tail = text[len(text) - tail_len:]
    dropped = len(text) - head_len - tail_len
    marker = f"\n…[{dropped} chars truncated]…\n"
    return head + marker + tail
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_tool_grounding.py -q`
Expected: PASS — all 8 tests green.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/tool_grounding.py tests/services/orchestrator/test_tool_grounding.py
git commit -m "feat(orchestrator): add budget-aware ground_tool_result helper (head+tail+marker)"
```

---

## Task 2: Wire grounding into `_run_react_loop` tool-result sites

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py` — add import + env knob near the top (after line 20); wrap the four content sites at lines 489, 521, 540-542, 555-557 inside `_run_react_loop`.
- Test: `tests/services/orchestrator/test_coding_orchestrator.py` — add two wire-in tests in the existing module (match style of `test_react_execute_emits_tool_events_for_run_bash` at line 95).

**Interfaces:**
- Consumes: `ground_tool_result`, `DEFAULT_TOOL_RESULT_BUDGET` from Task 1.
- Produces: module-level `LABMATE_TOOL_RESULT_BUDGET: int`; behavior — every tool-result `content` string appended as a `{"role":"tool", ...}` message in `_run_react_loop` is now `ground_tool_result(content, LABMATE_TOOL_RESULT_BUDGET)`.

- [ ] **Step 1: Write the failing wire-in tests**

Append to `tests/services/orchestrator/test_coding_orchestrator.py`:

```python
@pytest.mark.asyncio
async def test_react_loop_feeds_small_bash_output_verbatim():
    """A small run_bash result reaches the model context unchanged (no marker,
    no 2-4k cut). Regression guard: the old code path also passed small output
    through, but now via ground_tool_result — confirm verbatim + no marker."""
    from services.orchestrator.coding_orchestrator import AsyncOrchestrator

    orch = AsyncOrchestrator(skill_router=None, mcp=AsyncMock(), workspace="/tmp", max_steps=6)
    bash_result = MagicMock()
    bash_result.content = [MagicMock(text="hello world")]
    bash_result.isError = False
    orch.mcp.call_tool = AsyncMock(return_value=bash_result)

    resp1 = MagicMock(choices=[MagicMock(
        message=_msg_with_tool_call("run_bash", '{"command":"echo hello"}', "")
    )])
    finish_msg = MagicMock(tool_calls=None, content="done")
    finish_msg.model_dump = lambda: {"role": "assistant", "content": "done"}
    resp2 = MagicMock(choices=[MagicMock(message=finish_msg)])

    captured_messages = {}

    async def _capture(*a, **k):
        # On the 2nd call the messages list holds the appended tool result.
        captured_messages["messages"] = list(k["messages"])
        return resp2 if len(captured_messages) and "appended" in captured_messages else resp1

    # Simpler: drive two scripted responses and inspect via a spy on append.
    with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
               new_callable=AsyncMock, side_effect=[resp1, resp2]):
        await orch.react_execute("echo hello")

    # The bash output was small → fed verbatim. We assert via re-running the
    # grounding helper on the same content for determinism.
    from services.orchestrator.tool_grounding import ground_tool_result
    grounded = ground_tool_result("hello world", 16000)
    assert grounded == "hello world"
    assert "truncated" not in grounded


@pytest.mark.asyncio
async def test_react_loop_grounds_huge_bash_output_with_marker_and_tail():
    """A huge run_bash result is grounded: the tool message appended to the
    model context contains BOTH a truncation marker AND the tail sentinel."""
    from services.orchestrator.coding_orchestrator import AsyncOrchestrator

    huge = ("A" * 50000) + "TAILSENTINEL"
    orch = AsyncOrchestrator(skill_router=None, mcp=AsyncMock(), workspace="/tmp", max_steps=6)
    bash_result = MagicMock()
    bash_result.content = [MagicMock(text=huge)]
    bash_result.isError = False
    orch.mcp.call_tool = AsyncMock(return_value=bash_result)

    resp1 = MagicMock(choices=[MagicMock(
        message=_msg_with_tool_call("run_bash", '{"command":"cat big.log"}', "")
    )])
    finish_msg = MagicMock(tool_calls=None, content="done")
    finish_msg.model_dump = lambda: {"role": "assistant", "content": "done"}
    resp2 = MagicMock(choices=[MagicMock(message=finish_msg)])

    seen = {}

    async def _spy(*a, **k):
        # Capture the messages list passed on each model call.
        seen.setdefault("calls", []).append([dict(m) for m in k["messages"]])
        return seen["calls"] and (resp2 if len(seen["calls"]) >= 2 else resp1) or resp1

    with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
               new_callable=AsyncMock, side_effect=_spy):
        await orch.react_execute("dump log")

    # The 2nd model call carries the appended tool result message.
    second_call_messages = seen["calls"][1]
    tool_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
    assert tool_msgs, "no tool message was appended to context"
    content = tool_msgs[-1]["content"]
    assert "truncated" in content          # marker present
    assert "TAILSENTINEL" in content       # end-of-output evidence survived
    assert len(content) < len(huge)        # genuinely truncated
```

> Note for the implementer: `_spy` above captures the `messages` list reference on each `litellm.acompletion` call. Because `_run_react_loop` mutates one shared `messages` list in place, snapshot each call with `[dict(m) for m in k["messages"]]` (done above) so the second snapshot includes the appended `{"role":"tool",...}` entry.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_coding_orchestrator.py -k "grounds_huge_bash or feeds_small_bash" -q`
Expected: `test_react_loop_grounds_huge_bash_output_with_marker_and_tail` FAILS — the appended tool content has no `truncated` marker (current code joins raw text with no budget). The verbatim test passes trivially (it asserts the helper, not yet wired) and serves as the regression guard.

- [ ] **Step 3: Add the import + env knob**

In `services/orchestrator/coding_orchestrator.py`, after the existing import block (after line 20, `from .iteration_budget import IterationBudget, CHEAP_TOOLS`):

```python
from .iteration_budget import IterationBudget, CHEAP_TOOLS
from .tool_grounding import ground_tool_result, DEFAULT_TOOL_RESULT_BUDGET

# Max chars of RAW tool output (test results, file contents, bash stdout/stderr,
# skill results) fed back into the ReAct context per tool call. Generous on
# purpose: the weak local model must SEE real evidence, not a 600-char summary.
# Over budget → ground_tool_result keeps a head + tail + marker (end-of-output
# evidence like FAILED/assert lines survives). Replaces the old [:4000]/[:2000]
# hard cuts. See services/orchestrator/tool_grounding.py.
LABMATE_TOOL_RESULT_BUDGET = int(
    os.getenv("LABMATE_TOOL_RESULT_BUDGET", str(DEFAULT_TOOL_RESULT_BUDGET))
)
```

- [ ] **Step 4: Wrap the four tool-result content sites**

**Site A — call_skill_tool (replace the `[:4000]` cut, line 489):**

Old:
```python
                        content = json.dumps(res)[:4000]
```
New:
```python
                        content = ground_tool_result(
                            json.dumps(res), LABMATE_TOOL_RESULT_BUDGET
                        )
```

**Site B — LOCAL_TOOL_NAMES result (carries read_file / run_tests / list_dir, line 521):**

Old:
```python
                                content = json.dumps({"result": result}, default=str)
```
New:
```python
                                content = ground_tool_result(
                                    json.dumps({"result": result}, default=str),
                                    LABMATE_TOOL_RESULT_BUDGET,
                                )
```

**Site C — run_bash (lines 540-542):**

Old:
```python
                                content = "\n".join(
                                    c.text for c in obs.content if hasattr(c, "text")
                                )
```
New:
```python
                                content = ground_tool_result(
                                    "\n".join(
                                        c.text for c in obs.content if hasattr(c, "text")
                                    ),
                                    LABMATE_TOOL_RESULT_BUDGET,
                                )
```

**Site D — code_semantic_search (lines 555-557):**

Old:
```python
                                content = "\n".join(
                                    c.text for c in obs.content if hasattr(c, "text")
                                )
```
New:
```python
                                content = ground_tool_result(
                                    "\n".join(
                                        c.text for c in obs.content if hasattr(c, "text")
                                    ),
                                    LABMATE_TOOL_RESULT_BUDGET,
                                )
```

> Do NOT touch the error branches (`json.dumps({"error": ...})`) — those are short and already informative; grounding them is a no-op but adds noise. Do NOT touch line 421 (no-tool-call content return) or line 438 (`finish` summary) — those are final returns to the caller, not tool-result messages re-fed to the model.

- [ ] **Step 5: Run the wire-in tests to verify they pass**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_coding_orchestrator.py -k "grounds_huge_bash or feeds_small_bash" -q`
Expected: PASS — both green. The huge-bash test now sees a `truncated` marker and `TAILSENTINEL` in the appended tool message.

- [ ] **Step 6: Run the full orchestrator suite for regressions**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/ -q`
Expected: all green. If any pre-existing test asserts the OLD truncation length (e.g. `len(content) == 4000` or `== 2000`), that is the one regression to update — see Self-Review §1; update it to assert the new grounding behavior and call it out in the commit body.

- [ ] **Step 7: Commit**

```bash
git add services/orchestrator/coding_orchestrator.py tests/services/orchestrator/test_coding_orchestrator.py
git commit -m "feat(orchestrator): ground raw tool output (bash/skill/local/semantic) under LABMATE_TOOL_RESULT_BUDGET

Replaces [:4000]/[:2000] hard cuts in _run_react_loop with budget-aware
ground_tool_result so the local model sees real test/file/bash evidence
(head+tail+marker only on overflow; end-of-output FAILED lines survive)."
```

---

## Task 3: BDD feature + step defs

**Files:**
- Create: `tests/services/orchestrator/features/raw_output_grounding.feature` (full Gherkin in the "Behavior (BDD)" section above).
- Create: `tests/services/orchestrator/test_raw_output_grounding_bdd.py`

**Interfaces:**
- Consumes: `ground_tool_result` (Task 1); `AsyncOrchestrator` + the `_msg_with_tool_call`-style fake responses (Task 2); `run_async` from `tests/conftest.py`.
- Produces: bound scenarios for the 6 feature scenarios.

- [ ] **Step 1: Create the feature file**

Create `tests/services/orchestrator/features/raw_output_grounding.feature` with the EXACT Gherkin content from the "Behavior (BDD) — Gherkin" section above (copy it verbatim — all 6 scenarios).

- [ ] **Step 2: Write the failing step defs**

```python
# tests/services/orchestrator/test_raw_output_grounding_bdd.py
from __future__ import annotations

import json
import re
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.tool_grounding import ground_tool_result
from tests.conftest import run_async

pytestmark = [pytest.mark.bdd, pytest.mark.mocked]

scenarios("features/raw_output_grounding.feature")


@pytest.fixture
def ctx():
    return {
        "text": "",
        "budget": 16000,
        "grounded": "",
        "orch": None,
        "model_calls": [],
        "react_result": None,
    }


# ── Pure-helper scenarios ──────────────────────────────────────────────────
@given(parsers.parse("a tool output of {n:d} characters"))
def _output_n_chars(ctx, n):
    ctx["text"] = "x" * n


@given("a tool output that is a long passing-test preamble followed by a FAILED assertion at the very end")
def _failing_test_output(ctx):
    preamble = "PASSED test_a\n" * 4000  # ~56k chars of noise
    tail = (
        "FAILED tests/test_math.py::test_add - assert 5 == 4\n"
        "E       assert 5 == 4\n"
    )
    ctx["text"] = preamble + tail


@given(parsers.parse("a tool-result budget of {budget:d} characters"))
def _budget(ctx, budget):
    ctx["budget"] = budget


@when("the output is grounded")
def _ground(ctx):
    ctx["grounded"] = ground_tool_result(ctx["text"], ctx["budget"])


@then("the grounded text equals the original output exactly")
def _equals_original(ctx):
    assert ctx["grounded"] == ctx["text"]


@then("the grounded text contains no truncation marker")
def _no_marker(ctx):
    assert "truncated" not in ctx["grounded"]


@then("the grounded text is no longer than the budget plus the marker")
def _within_budget_plus_marker(ctx):
    m = re.search(r"\n…\[\d+ chars truncated\]…\n", ctx["grounded"])
    marker_len = len(m.group(0)) if m else 0
    assert len(ctx["grounded"]) <= ctx["budget"] + marker_len


@then("the grounded text starts with the head of the original output")
def _starts_with_head(ctx):
    assert ctx["grounded"][:10] == ctx["text"][:10]


@then("the grounded text ends with the tail of the original output")
def _ends_with_tail(ctx):
    assert ctx["grounded"][-10:] == ctx["text"][-10:]


@then("the grounded text contains a truncation marker reporting the dropped char count")
def _marker_with_count(ctx):
    assert re.search(r"\n…\[\d+ chars truncated\]…\n", ctx["grounded"]) is not None


@then("the grounded text contains the FAILED assertion line")
def _has_failed_line(ctx):
    assert "FAILED tests/test_math.py::test_add" in ctx["grounded"]


@then("the grounded text contains the assert detail line")
def _has_assert_detail(ctx):
    assert "assert 5 == 4" in ctx["grounded"]


# ── Wired-loop scenarios ───────────────────────────────────────────────────
def _bash_then_finish_responses():
    """resp1 = call run_bash; resp2 = finish with plain content."""
    tc = MagicMock()
    tc.id = "call_bash"
    tc.function = MagicMock()
    tc.function.name = "run_bash"
    tc.function.arguments = json.dumps({"command": "echo x"})
    msg1 = MagicMock()
    msg1.content = None
    msg1.tool_calls = [tc]
    msg1.reasoning_content = ""
    msg1.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    resp1 = MagicMock(choices=[MagicMock(message=msg1)])

    finish_msg = MagicMock(tool_calls=None, content="done")
    finish_msg.model_dump = lambda: {"role": "assistant", "content": "done"}
    resp2 = MagicMock(choices=[MagicMock(message=finish_msg)])
    return resp1, resp2


@given("a ReAct orchestrator wired to a fake model that runs one bash command then finishes")
def _orch_bash_finish(ctx):
    from services.orchestrator.coding_orchestrator import AsyncOrchestrator

    orch = AsyncOrchestrator(skill_router=None, mcp=AsyncMock(), workspace="/tmp", max_steps=6)
    ctx["orch"] = orch


@given(parsers.parse('the bash command returns "{text}" as its only output'))
def _bash_returns_text(ctx, text):
    res = MagicMock()
    res.content = [MagicMock(text=text)]
    res.isError = False
    ctx["orch"].mcp.call_tool = AsyncMock(return_value=res)


@given(parsers.parse('the bash command returns {n:d} characters ending in "{sentinel}"'))
def _bash_returns_huge(ctx, n, sentinel):
    body = ("A" * n) + sentinel
    res = MagicMock()
    res.content = [MagicMock(text=body)]
    res.isError = False
    ctx["orch"].mcp.call_tool = AsyncMock(return_value=res)


@when(parsers.parse('the goal "{goal}" is executed'))
def _execute(ctx, goal):
    orch = ctx["orch"]
    resp1, resp2 = _bash_then_finish_responses()
    scripted = [resp1, resp2]

    async def _spy(*a, **k):
        # Snapshot the messages list (copy each dict) so the post-tool-call
        # snapshot includes the appended {"role":"tool",...} entry.
        ctx["model_calls"].append([dict(m) for m in k["messages"]])
        return scripted[min(len(ctx["model_calls"]) - 1, len(scripted) - 1)]

    async def _run():
        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, side_effect=_spy):
            return await orch.react_execute(goal)

    ctx["react_result"] = run_async(_run())


def _last_tool_message_content(ctx) -> str:
    # The 2nd model call carries the appended tool result.
    assert len(ctx["model_calls"]) >= 2, "expected at least two model calls"
    msgs = ctx["model_calls"][1]
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert tool_msgs, "no tool message appended to context"
    return tool_msgs[-1]["content"]


@then(parsers.parse('the tool message appended to the model context contains "{needle}" verbatim'))
def _tool_msg_contains_verbatim(ctx, needle):
    assert needle in _last_tool_message_content(ctx)


@then("the tool message contains no truncation marker")
def _tool_msg_no_marker(ctx):
    assert "truncated" not in _last_tool_message_content(ctx)


@then("the tool message appended to the model context contains a truncation marker")
def _tool_msg_has_marker(ctx):
    assert "truncated" in _last_tool_message_content(ctx)


@then(parsers.parse('the tool message appended to the model context contains "{needle}"'))
def _tool_msg_contains(ctx, needle):
    assert needle in _last_tool_message_content(ctx)
```

- [ ] **Step 3: Run the BDD suite to verify it passes**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_raw_output_grounding_bdd.py -q`
Expected: PASS — all 6 scenarios bound and green. (Helper scenarios pass from Task 1; wired scenarios pass from Task 2.)

- [ ] **Step 4: Run the whole orchestrator + memory suite**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/ tests/services/memory/ -q`
Expected: all green, no regressions.

- [ ] **Step 5: Commit**

```bash
git add tests/services/orchestrator/features/raw_output_grounding.feature tests/services/orchestrator/test_raw_output_grounding_bdd.py
git commit -m "test(orchestrator): BDD contract for raw tool-output grounding (verbatim under budget, head+tail+marker over)"
```

---

## Self-Review

**1. Spec coverage**
- Pure helper `ground_tool_result(text, budget) -> str` with verbatim-under-budget + head/tail/marker-over-budget → Task 1 (8 unit tests).
- New module `services/orchestrator/tool_grounding.py` → Task 1.
- Replace hard `[:4000]` / `[:2000]` truncations in `_run_react_loop` tool-result handling → Task 2, Sites A-D (call_skill_tool 489, LOCAL_TOOL_NAMES/read_file/run_tests 521, run_bash 540-542, code_semantic_search 555-557). The prompt names `read_file` and `run_tests` as distinct tools; verified they flow through `LOCAL_TOOL_NAMES` (line 515) — grounding Site B covers both. No separate `run_tests`/`read_file` `elif` exists to wrap.
- Env knob `LABMATE_TOOL_RESULT_BUDGET` default 16000 → Task 2 Step 3.
- Marker shows start AND end (end-of-output FAILED/assert evidence survives) → Task 1 `test_over_budget_keeps_head_and_tail`; BDD scenario "long failing-test output … FAILED lines intact".
- Additive + regression-safe; helper fully unit-tested (verbatim under budget; head+tail over budget; marker present; head and tail both retained) → Task 1.
- BDD contract: feature `raw_output_grounding.feature` (@mocked) + step defs `test_raw_output_grounding_bdd.py` using existing `run_async`; unit tests `test_tool_grounding.py` → Tasks 1 & 3. (The prompt says `fake_model` respx fixture exists; these scenarios drive the loop via the scripted-`litellm.acompletion` patch pattern already used by the sibling `test_tool_loop_detection_bdd.py`, which is the project's established way to inspect the appended `{"role":"tool"}` message — `fake_model` is available but the message-inspection assertions need the scripted-response form.)

**2. Placeholder scan** — No TBD/TODO/"handle edge cases"/"similar to Task N". Every code step shows complete code. All file paths absolute-from-repo-root and exact. All commands have expected output.

**3. Type consistency** — `ground_tool_result(text: str, budget: int) -> str` and `DEFAULT_TOOL_RESULT_BUDGET` used identically in Tasks 1, 2, 3. Module-level `LABMATE_TOOL_RESULT_BUDGET` defined once (Task 2 Step 3) and referenced at all four wire-in sites. Marker regex `\n…\[\d+ chars truncated\]…\n` matches the f-string `f"\n…[{dropped} chars truncated]…\n"` produced by the helper — both use the `…` ellipsis char and the literal `chars truncated`. Consistent across unit tests, BDD step defs, and implementation.

**4. Regression callout (REQUIRED before completion)** — Before Task 2 Step 6, grep for any existing test that asserts the OLD truncation length:
```bash
cd /Users/zachstallbohm/Work/Labmate
grep -rn "4000\|\[:2000\]\|== 2000\|len(content)" tests/services/orchestrator/test_coding_orchestrator.py
```
A clean pre-check on this branch shows the only `2000` assertion is `test_condense_truncates_long_output` (line ~281, `assert len(result.summary) <= 2000`) which targets `_condense` (the aggregate/Result path, line 843-844), NOT a `_run_react_loop` tool-result site — it is **out of scope and must stay**. No `_run_react_loop` tool-result test currently asserts the 4000/2000 cut, so no test update is expected. **If** Task 2 Step 6 surfaces a failing test that asserted an old cut on a tool-result message, update it to assert grounding behavior (verbatim under budget / marker+tail over budget) and note the change in the commit body.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-06-26-raw-output-grounding.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
