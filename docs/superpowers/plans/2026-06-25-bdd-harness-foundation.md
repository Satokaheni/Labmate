# BDD Harness Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up pytest-bdd as the repo's behavioral source of truth, with a shared `fake_model` respx fixture and a smoke feature that proves the harness wiring works end to end.

**Architecture:** pytest-bdd is layered on top of the existing pytest suite without disturbing it. A new repo-root `tests/conftest.py` provides a shared `fake_model` fixture that mocks the OpenAI-compatible inference seam (`POST http://localhost:8000/v1/chat/completions`) with respx — the same HTTP endpoint litellm hits via `api_base="http://localhost:8000/v1"`. Gherkin `.feature` files live under `tests/services/orchestrator/features/`, and their step definitions live beside the existing unit tests as `test_<slug>_bdd.py`. This task establishes the **Shared BDD Contract** that every later feature plan references; it adds zero production code.

**Tech Stack:** pytest 8, pytest-asyncio (`asyncio_mode = auto`), pytest-bdd ≥ 7.0, respx ≥ 0.21 (installed: 0.23.1), httpx (installed: 0.28.1), litellm (the orchestrator's OpenAI client seam).

## Global Constraints

- **Dependency floors (copy verbatim into `requirements.txt`):** `pytest-bdd>=7.0`. `respx>=0.21` is **already present** in `services/orchestrator/requirements.txt` (line 18, currently pinned to the installed 0.23.1 family) — do NOT duplicate it.
- **Inference seam URL is fixed:** the orchestrator calls `litellm.acompletion(model="openai/gemma-4-31b", api_base="http://localhost:8000/v1", ...)`, which issues `POST http://localhost:8000/v1/chat/completions`. The respx mock MUST target exactly `http://localhost:8000/v1/chat/completions`. Verified against `services/orchestrator/graph.py:21` (`GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")`) and `services/orchestrator/skill_router.py:109,296`.
- **Never use tiktoken** anywhere in test or fixture code; token counting is Gemma SentencePiece only (not exercised in this plan, but the rule stands).
- **Marker discipline:** the repo already registers `mocked` and `live` markers in **two** places — `pytest.ini` (lines 3-5) and `tests/services/orchestrator/conftest.py:7-9` (`pytest_configure`). Keep both. Add the `bdd` marker to `pytest.ini` only.
- **CI-safe default:** every BDD scenario added by this plan or any later plan MUST be tagged `@mocked` unless it genuinely needs live inference (`@live`). `@mocked` scenarios must never touch a GPU, a real subprocess, or the network.
- **Test invocation is always** `PYTHONPATH=. python -m pytest ...` from the repo root (`/Users/zachstallbohm/Work/Labmate`). There is no installed package; imports resolve via `PYTHONPATH=.`.
- **Do NOT modify** `core/`, `tools/`, `main.py`, `services/orchestrator/coding_orchestrator.py`, or any production module. This is a test-harness-only change.
- **pytest-bdd tag → marker mapping:** pytest-bdd converts a Gherkin tag `@mocked` into the pytest marker `mocked` automatically (it calls `pytest.mark.<tag>` per scenario). This is why the `mocked`/`live`/`bdd` markers must be registered — otherwise `--strict-markers` (if ever enabled) and `-W error` would error on unknown marks.

---

## File Map

| File | Responsibility | Action |
|------|----------------|--------|
| `services/orchestrator/requirements.txt` | Python deps for the orchestrator + its tests | Modify — add `pytest-bdd>=7.0` |
| `pytest.ini` | Markers + asyncio mode | Modify — add `bdd` marker |
| `tests/conftest.py` | **Repo-root shared fixtures.** Defines `fake_model` (respx HTTP-seam mock). New file. | Create |
| `tests/services/orchestrator/features/` | Conventional home for all orchestrator `.feature` files | Create (directory) |
| `tests/services/orchestrator/features/smoke.feature` | Trivial Given/When/Then proving pytest-bdd is wired | Create |
| `tests/services/orchestrator/test_smoke_bdd.py` | Step defs binding the smoke feature; also exercises `fake_model` end to end against the real litellm seam | Create |
| `docs/superpowers/plans/2026-06-25-bdd-harness-foundation.md` | This plan (records the Shared BDD Contract) | Create (already being written) |

**Why a repo-root `tests/conftest.py` and not the existing `tests/services/orchestrator/conftest.py`?** pytest merges conftests up the directory tree. The `fake_model` fixture must be visible to any future `.feature` directory anywhere under `tests/`, so it belongs at `tests/conftest.py`. The existing orchestrator conftest keeps its mongo/chroma/redis/storage fixtures untouched.

---

## Shared BDD Contract (DEFINED HERE — every later feature plan references this)

This task **defines** the contract below. Later "feature" plans (e.g. memory-write-triggers BDD, routing BDD) MUST conform to it verbatim — do not re-derive it.

1. **Feature files:** `tests/services/orchestrator/features/<slug>.feature`. `<slug>` is kebab-case matching the feature under test (e.g. `memory-write-triggers.feature`). Every `Scenario`/`Scenario Outline` is tagged `@mocked` (CI-safe, no GPU/network/subprocess) unless it requires live inference, in which case it is tagged `@live` and skipped in CI.
2. **Step-definition files:** `tests/services/orchestrator/test_<slug>_bdd.py`, where `<slug>` is the feature file's slug with hyphens converted to underscores. Examples: feature `smoke.feature` → `test_smoke_bdd.py`; feature `memory-write-triggers.feature` → `test_memory_write_triggers_bdd.py`. Each step-def file begins with `from pytest_bdd import scenarios, given, when, then, parsers` and binds the feature via `scenarios("features/<slug>.feature")` (path relative to the step-def file's directory).
3. **Shared model fixture:** `fake_model` lives in `tests/conftest.py` (defined in this plan). Signature: `fake_model(...)` returns a callable `_set(tool_name: str | None, arguments: dict | None = None, *, content: str | None = None) -> None`. Calling `_set("edit_file", {"path": "a.py"})` programs the next inference response to be a tool call; calling `_set(None, content="hello")` programs a plain-content (no tool call) completion. It mocks `POST http://localhost:8000/v1/chat/completions`.
4. **Unit (TDD) tests** sit beside the existing ones as `tests/services/orchestrator/test_<feature>.py` (e.g. `test_storage_manager.py`) — unchanged convention.
5. **Marker names:** `mocked`, `live`, `bdd` (registered in `pytest.ini`). Gherkin `@mocked`/`@live` tags map onto the `mocked`/`live` pytest markers automatically.

---

### Task 1: Add pytest-bdd dependency

**Files:**
- Modify: `services/orchestrator/requirements.txt:19` (append after `respx>=0.21`)

**Interfaces:**
- Consumes: nothing.
- Produces: the importable `pytest_bdd` package (provides `scenarios`, `given`, `when`, `then`, `parsers`) used by every step-def file in this and later plans.

- [ ] **Step 1: Confirm pytest-bdd is currently absent (failing precondition)**

Run: `cd /Users/zachstallbohm/Work/Labmate && python -c "import pytest_bdd"`
Expected: `ModuleNotFoundError: No module named 'pytest_bdd'`

- [ ] **Step 2: Append the dependency**

Append a new final line to `services/orchestrator/requirements.txt` (the file currently ends at line 18 with `respx>=0.21`):

```text
pytest-bdd>=7.0
```

The full tail of the file must now read:

```text
pytest>=8.0
pytest-asyncio>=0.23
respx>=0.21
pytest-bdd>=7.0
```

- [ ] **Step 3: Install it**

Run: `cd /Users/zachstallbohm/Work/Labmate && python -m pip install "pytest-bdd>=7.0"`
Expected: ends with `Successfully installed ... pytest-bdd-7.x.x ...` (a `parse`/`parse_type`/`Mako` dependency may also install — that is fine).

- [ ] **Step 4: Verify the import now succeeds**

Run: `cd /Users/zachstallbohm/Work/Labmate && python -c "import pytest_bdd; from pytest_bdd import scenarios, given, when, then, parsers; print('pytest_bdd', pytest_bdd.__version__)"`
Expected: a line like `pytest_bdd 7.3.0` and exit code 0.

- [ ] **Step 5: Commit**

```bash
cd /Users/zachstallbohm/Work/Labmate
git add services/orchestrator/requirements.txt
git commit -m "test(bdd): add pytest-bdd>=7.0 dependency"
```

---

### Task 2: Register the `bdd` marker

**Files:**
- Modify: `pytest.ini:1-6`

**Interfaces:**
- Consumes: nothing.
- Produces: a registered `bdd` marker (alongside existing `mocked`, `live`) so future `@pytest.mark.bdd` / Gherkin `@bdd` tags do not warn under `-W error::pytest.PytestUnknownMarkWarning`.

- [ ] **Step 1: Write a failing assertion that the marker is unregistered**

Run: `cd /Users/zachstallbohm/Work/Labmate && python -m pytest --markers | grep -c "@pytest.mark.bdd"`
Expected: `0` (the marker does not yet exist).

- [ ] **Step 2: Add the marker to `pytest.ini`**

The file currently is:

```ini
[pytest]
asyncio_mode = auto
markers =
    mocked: runs without real subprocesses or GPU
    live: requires a real subprocess / inference server
```

Replace the whole file with (adds the `bdd` line, keeps the other two verbatim):

```ini
[pytest]
asyncio_mode = auto
markers =
    mocked: runs without real subprocesses or GPU
    live: requires a real subprocess / inference server
    bdd: pytest-bdd scenario (Gherkin .feature backed); inherits @mocked or @live
```

- [ ] **Step 3: Verify the marker is now registered**

Run: `cd /Users/zachstallbohm/Work/Labmate && python -m pytest --markers | grep "@pytest.mark.bdd"`
Expected: a line `@pytest.mark.bdd: pytest-bdd scenario (Gherkin .feature backed); inherits @mocked or @live`

- [ ] **Step 4: Confirm existing markers still register (no regression)**

Run: `cd /Users/zachstallbohm/Work/Labmate && python -m pytest --markers | grep -E "@pytest.mark.(mocked|live)"`
Expected: two lines, one for `mocked` and one for `live`.

- [ ] **Step 5: Commit**

```bash
cd /Users/zachstallbohm/Work/Labmate
git add pytest.ini
git commit -m "test(bdd): register bdd marker in pytest.ini"
```

---

### Task 3: Create the shared `fake_model` respx fixture

**Files:**
- Create: `tests/conftest.py`
- Test (proves the fixture wiring without any feature file): `tests/services/orchestrator/test_fake_model_fixture.py`

**Interfaces:**
- Consumes: `respx_mock` (provided by the installed `respx` pytest plugin), `httpx.Response`.
- Produces: the **`fake_model`** fixture. It yields a callable:
  ```python
  _set(tool_name: str | None, arguments: dict | None = None, *, content: str | None = None) -> None
  ```
  - `_set("edit_file", {"path": "a.py", "content": "x"})` → programs the seam to return an assistant message with one `tool_calls` entry (`finish_reason="tool_calls"`).
  - `_set(None, content="2 + 2 = 4")` → programs the seam to return a plain assistant `content` message (`finish_reason="stop"`, no `tool_calls`).
  - Targets `POST http://localhost:8000/v1/chat/completions`.
  Every later BDD/TDD plan that needs a deterministic model response consumes this exact fixture.

- [ ] **Step 1: Write the failing test for the fixture**

Create `tests/services/orchestrator/test_fake_model_fixture.py`:

```python
"""Proves the shared fake_model fixture mocks the inference HTTP seam.

The orchestrator talks to llama.cpp via litellm with
api_base="http://localhost:8000/v1" and model="openai/gemma-4-31b",
which issues POST http://localhost:8000/v1/chat/completions. These tests
exercise that exact seam through litellm so the fixture is validated against
the real call path, not a hand-rolled httpx request.
"""
from __future__ import annotations

import json
import pytest
import litellm


@pytest.mark.mocked
async def test_fake_model_programs_a_tool_call(fake_model):
    fake_model("edit_file", {"path": "src/app.py", "content": "print(1)"})

    resp = await litellm.acompletion(
        model="openai/gemma-4-31b",
        api_base="http://localhost:8000/v1",
        api_key="not-needed",
        messages=[{"role": "user", "content": "edit the file"}],
    )

    tool_calls = resp.choices[0].message.tool_calls
    assert tool_calls is not None
    assert tool_calls[0].function.name == "edit_file"
    assert json.loads(tool_calls[0].function.arguments) == {
        "path": "src/app.py",
        "content": "print(1)",
    }


@pytest.mark.mocked
async def test_fake_model_programs_plain_content(fake_model):
    fake_model(None, content="2 + 2 = 4")

    resp = await litellm.acompletion(
        model="openai/gemma-4-31b",
        api_base="http://localhost:8000/v1",
        api_key="not-needed",
        messages=[{"role": "user", "content": "what is 2+2?"}],
    )

    msg = resp.choices[0].message
    assert msg.tool_calls in (None, [])
    assert msg.content == "2 + 2 = 4"
```

- [ ] **Step 2: Run it to confirm it fails (no fixture yet)**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_fake_model_fixture.py -v`
Expected: FAIL — both tests error with `fixture 'fake_model' not found`.

- [ ] **Step 3: Create the `tests/conftest.py` fixture**

Create `tests/conftest.py`:

```python
"""Repo-root shared fixtures for the Labmate pytest + pytest-bdd suite.

Defined here (not in tests/services/orchestrator/conftest.py) so the
fixtures are visible to every .feature directory anywhere under tests/.

Fixtures:
  - fake_model : respx mock of the OpenAI-compatible inference seam
                 (POST http://localhost:8000/v1/chat/completions). This is
                 the exact URL litellm hits when the orchestrator calls
                 acompletion(api_base="http://localhost:8000/v1").

Shared BDD Contract: see
docs/superpowers/plans/2026-06-25-bdd-harness-foundation.md
"""
from __future__ import annotations

import json

import httpx
import pytest

# The inference seam every orchestrator model call routes through.
# Source of truth: services/orchestrator/graph.py GEMMA_BASE default.
INFERENCE_COMPLETIONS_URL = "http://localhost:8000/v1/chat/completions"


@pytest.fixture
def fake_model(respx_mock):
    """Program the inference seam to return a deterministic completion.

    Returns a callable used inside @given steps (or directly in a test)
    before the agent issues a model call:

        # tool-call completion
        fake_model("edit_file", {"path": "src/app.py", "content": "..."})

        # plain-content completion (no tool call)
        fake_model(None, content="2 + 2 = 4")

    The last call wins: re-calling re-programs the same route.
    """

    def _set(
        tool_name: str | None,
        arguments: dict | None = None,
        *,
        content: str | None = None,
    ) -> None:
        if tool_name is not None:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_test",
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps(arguments or {}),
                        },
                    }
                ],
            }
            finish_reason = "tool_calls"
        else:
            message = {"role": "assistant", "content": content or ""}
            finish_reason = "stop"

        body = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": "gemma4-local",
            "choices": [
                {"index": 0, "message": message, "finish_reason": finish_reason}
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        }
        respx_mock.post(INFERENCE_COMPLETIONS_URL).mock(
            return_value=httpx.Response(200, json=body)
        )

    return _set
```

- [ ] **Step 4: Run the fixture test to confirm it passes**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_fake_model_fixture.py -v`
Expected: PASS — `test_fake_model_programs_a_tool_call PASSED` and `test_fake_model_programs_plain_content PASSED`.

> If litellm raises about an unmocked route (e.g. it probes a `/models` endpoint), re-run with the route assertion relaxed by adding `respx_mock` config — but with litellm + `api_base` set, only `/chat/completions` is called, so this should pass as written. Do NOT add network access to make it pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/zachstallbohm/Work/Labmate
git add tests/conftest.py tests/services/orchestrator/test_fake_model_fixture.py
git commit -m "test(bdd): add shared fake_model respx fixture in tests/conftest.py"
```

---

### Task 4: Smoke feature + step definitions (prove pytest-bdd runs)

**Files:**
- Create: `tests/services/orchestrator/features/smoke.feature`
- Create: `tests/services/orchestrator/test_smoke_bdd.py`

**Interfaces:**
- Consumes: `scenarios`, `given`, `when`, `then`, `parsers` from `pytest_bdd`; the `fake_model` fixture from Task 3; `litellm.acompletion`.
- Produces: nothing other plans import — this is the canonical worked example later feature plans copy the structure from. It establishes that the feature-dir + `test_<slug>_bdd.py` + `@mocked` wiring executes.

- [ ] **Step 1: Create the feature directory marker check (precondition)**

Run: `cd /Users/zachstallbohm/Work/Labmate && ls tests/services/orchestrator/features 2>&1`
Expected: `ls: ... features: No such file or directory` (the directory does not exist yet).

- [ ] **Step 2: Write the smoke feature file**

Create `tests/services/orchestrator/features/smoke.feature`:

```gherkin
@mocked
Feature: BDD harness smoke test
  As the Labmate test suite
  I want pytest-bdd to bind a Gherkin scenario to Python steps
  So that later feature plans can rely on the harness wiring

  Scenario: a programmed model returns the answer we set
    Given the model is programmed to answer "2 plus 2 is 4"
    When the orchestrator asks the model a question
    Then the model reply is "2 plus 2 is 4"

  Scenario: a programmed model returns a tool call
    Given the model is programmed to call tool "list_dir" with path "."
    When the orchestrator asks the model a question
    Then the model requests tool "list_dir"
```

- [ ] **Step 3: Write the step-definition file (will fail — no steps bound yet)**

Create `tests/services/orchestrator/test_smoke_bdd.py`:

```python
"""Step definitions for the BDD harness smoke feature.

Canonical worked example for the Shared BDD Contract:
  - feature lives at features/smoke.feature (tagged @mocked)
  - step-def file is test_<slug>_bdd.py beside the unit tests
  - scenarios(...) path is relative to THIS file's directory
  - the @mocked Gherkin tag maps to the pytest 'mocked' marker

See docs/superpowers/plans/2026-06-25-bdd-harness-foundation.md
"""
from __future__ import annotations

import json

import litellm
import pytest
from pytest_bdd import scenarios, given, when, then, parsers

# Bind every Scenario in smoke.feature to the step defs below.
scenarios("features/smoke.feature")


@pytest.fixture
def reply_box() -> dict:
    """Per-scenario mutable holder for the model response."""
    return {}


@given(parsers.parse('the model is programmed to answer "{answer}"'))
def _program_plain_answer(fake_model, answer: str) -> None:
    fake_model(None, content=answer)


@given(
    parsers.parse('the model is programmed to call tool "{tool}" with path "{path}"')
)
def _program_tool_call(fake_model, tool: str, path: str) -> None:
    fake_model(tool, {"path": path})


@when("the orchestrator asks the model a question")
def _ask_model(reply_box: dict) -> None:
    async def _call():
        return await litellm.acompletion(
            model="openai/gemma-4-31b",
            api_base="http://localhost:8000/v1",
            api_key="not-needed",
            messages=[{"role": "user", "content": "anything"}],
        )

    import asyncio

    reply_box["resp"] = asyncio.get_event_loop().run_until_complete(_call())


@then(parsers.parse('the model reply is "{expected}"'))
def _assert_reply(reply_box: dict, expected: str) -> None:
    msg = reply_box["resp"].choices[0].message
    assert msg.content == expected


@then(parsers.parse('the model requests tool "{tool}"'))
def _assert_tool(reply_box: dict, tool: str) -> None:
    tool_calls = reply_box["resp"].choices[0].message.tool_calls
    assert tool_calls is not None
    assert tool_calls[0].function.name == tool
    # arguments are valid JSON
    json.loads(tool_calls[0].function.arguments)
```

> **Note on the `@when` step:** `asyncio_mode = auto` makes `async def` *test functions* run on an event loop, but pytest-bdd step functions are plain sync callables — they are not collected as tests. So the async model call is driven explicitly with `run_until_complete`. This is the contract pattern later plans reuse for steps that make async calls. (If a later step needs concurrency it can use `asyncio.run(...)` instead; `run_until_complete` is used here because respx's mock router is already active on the running loop.)

- [ ] **Step 4: Run the smoke scenarios**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_smoke_bdd.py -v`
Expected: PASS — two scenarios collected and passed, e.g.:
```
tests/services/orchestrator/test_smoke_bdd.py::test_a_programmed_model_returns_the_answer_we_set PASSED
tests/services/orchestrator/test_smoke_bdd.py::test_a_programmed_model_returns_a_tool_call PASSED
```

> If you instead see `StepDefinitionNotFoundError`, the `parsers.parse(...)` text does not byte-match the Gherkin step — make the step strings identical to the `.feature` lines (quotes included).

- [ ] **Step 5: Confirm the `@mocked` tag became the `mocked` marker**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_smoke_bdd.py -m mocked -v`
Expected: both scenarios still selected and PASS (proves the Gherkin `@mocked` tag maps to the registered pytest `mocked` marker).

Then prove the negative:

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_smoke_bdd.py -m live -v`
Expected: `2 deselected` (or `no tests ran` after deselection) — neither scenario is tagged `@live`.

- [ ] **Step 6: Commit**

```bash
cd /Users/zachstallbohm/Work/Labmate
git add tests/services/orchestrator/features/smoke.feature tests/services/orchestrator/test_smoke_bdd.py
git commit -m "test(bdd): smoke feature + step defs proving pytest-bdd harness wiring"
```

---

### Task 5: Full-suite regression + harness sign-off

**Files:**
- None created/modified. Verification only.

**Interfaces:**
- Consumes: everything from Tasks 1–4.
- Produces: evidence the new harness coexists with the existing 340+ orchestrator tests.

- [ ] **Step 1: Run the new harness files together**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_smoke_bdd.py tests/services/orchestrator/test_fake_model_fixture.py -v`
Expected: 4 passed (2 fixture tests + 2 smoke scenarios), 0 failed.

- [ ] **Step 2: Run the full orchestrator test directory to confirm no regression**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/ -q 2>&1 | tail -15`
Expected: the summary line shows the pre-existing passing count **plus the 4 new tests**, with `0 failed` attributable to this change. (If unrelated pre-existing failures exist, they must be identical to a `git stash`-clean baseline — this plan adds no production code, so it cannot break orchestrator logic.)

- [ ] **Step 3: Confirm marker-filtered CI invocation works**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/ -m mocked -q 2>&1 | tail -5`
Expected: a passing summary; the two smoke scenarios and two fixture tests are included in the `mocked` selection.

- [ ] **Step 4: Confirm no warnings about unknown marks**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_smoke_bdd.py -W error::pytest.PytestUnknownMarkWarning -q`
Expected: PASS with no `PytestUnknownMarkWarning` raised (proves `mocked` and `bdd` are registered).

- [ ] **Step 5: Commit a no-op marker (documentation) if anything was adjusted, else stop**

If Steps 1–4 all pass with the code already committed in Tasks 1–4, there is nothing to commit — the harness is done. If a step required a fix, commit it:

```bash
cd /Users/zachstallbohm/Work/Labmate
git add -A
git commit -m "test(bdd): finalize harness foundation (regression-clean)"
```

---

## Self-Review

**1. Spec coverage** (against the feature's five required deliverables):

| Required deliverable | Task |
|---|---|
| Add `pytest-bdd>=7.0` and `respx>=0.21` to orchestrator requirements | Task 1 (pytest-bdd added; respx already present at line 18 — noted in Global Constraints, not duplicated) |
| Register markers in `pytest.ini`; keep `mocked`/`live`; document `@mocked`/`@live` tag usage | Task 2 (adds `bdd`, keeps the other two verbatim); tag→marker mapping documented in Global Constraints and verified in Task 4 Step 5 |
| Create shared `fake_model` respx fixture in `tests/conftest.py` mocking `POST http://localhost:8000/v1/chat/completions`, programmable tool-call OR plain-content, against the real base URL | Task 3 (URL verified against `graph.py:21` / `skill_router.py`; full fixture code given; both modes covered) |
| Create `features/` dir + `smoke.feature` + `test_smoke_bdd.py` proving pytest-bdd runs | Task 4 |
| Document the Shared BDD Contract | Dedicated "Shared BDD Contract" section + the contract is exercised in Task 4 |

All five covered. No gaps.

**2. Placeholder scan:** No "TBD"/"TODO"/"handle edge cases"/"similar to Task N". Every code step contains complete, runnable code. Every run step has an exact command and expected output. ✔

**3. Type consistency:**
- `fake_model` callable signature `_set(tool_name: str | None, arguments: dict | None = None, *, content: str | None = None)` is defined identically in the Shared BDD Contract (item 3), Task 3 Interfaces, Task 3 Step 3 code, and consumed exactly that way in Task 3 Step 1 (`fake_model("edit_file", {...})`, `fake_model(None, content=...)`) and Task 4's step defs (`fake_model(None, content=answer)`, `fake_model(tool, {"path": path})`). Consistent. ✔
- The seam URL constant `http://localhost:8000/v1/chat/completions` is identical in Global Constraints, the contract, and the fixture (`INFERENCE_COMPLETIONS_URL`). ✔
- Feature→step-def naming (`smoke.feature` → `test_smoke_bdd.py`; `memory-write-triggers.feature` → `test_memory_write_triggers_bdd.py`) is stated once in the contract and followed in Task 4. ✔

**One correction applied during review:** Shared BDD Contract item 2 originally implied a rigid hyphen→underscore rule then hedged it; reworded to a single unambiguous mapping (`<slug>.feature` → `test_<slug-with-underscores>_bdd.py`) with two concrete examples, eliminating the contradiction.

No remaining issues.
