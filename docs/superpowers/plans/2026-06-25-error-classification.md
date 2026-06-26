# Error Classification Before Retry — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the crude `_NONRETRYABLE_ERROR_MARKERS` substring tuple in `graph.py` with a proper, pure, deterministic error classifier (`services/orchestrator/error_classifier.py`) so the orchestrator skips reflect/retry on terminal/environmental failures (no Docker, missing API key, network down) — the documented #1 latency sink on this single-GPU pod — while still retrying genuinely-transient and unknown failures up to `MAX_GOAL_ATTEMPTS`.

**Architecture:** A new pure module exposes an `ErrorClass` enum and `classify_error(error: str | Exception) -> ErrorClass`. The classifier matches case-insensitive, ordered substring patterns. The execute node and `router()` consult the class instead of a boolean substring test: terminal classes (`TERMINAL_DEPENDENCY`, `TERMINAL_CREDENTIAL`, `TERMINAL_NETWORK`) are marked exhausted immediately (no reflect pass); `RATE_LIMITED` gets a bounded backoff and limited retries; `TRANSIENT` gets limited retries; `RETRYABLE` (default/unknown) keeps today's behavior of retrying up to `MAX_GOAL_ATTEMPTS`. One additive `State` field (`error_class`) records the decision for observability.

**Tech Stack:** Python 3.11+, LangGraph `StateGraph`, pytest (`@pytest.mark.mocked`), pytest-bdd (Scenario Outline + Examples), `respx` (mocks the llama.cpp OpenAI endpoint via the shared `fake_model` fixture).

## Global Constraints

- Python files use `snake_case.py`; classes use `PascalCase`; functions use `snake_case`.
- Do NOT modify `core/`, `tools/`, or `main.py` — M2 baseline must stay runnable.
- Never import `tiktoken`; never use `chromadb.PersistentClient`.
- All tunables read from `os.getenv` with the existing defaults; do not change `MAX_GOAL_ATTEMPTS` default (`2`), `MAX_VERIFY_RETRIES` default (`1`).
- The classifier must be PURE (no I/O, no env reads inside the hot path beyond module-load caps), case-INSENSITIVE, and DETERMINISTIC.
- New tests carry `@pytest.mark.mocked`. The feature file is tagged `@mocked`.
- Assume the foundation plan already added `pytest-bdd` to the test deps and a `fake_model` respx fixture in `tests/conftest.py`. This plan adds no `fake_model` use of its own (the classifier is pure), but the BDD step defs depend on `pytest_bdd` being importable.
- Preserve current behavior for unknown errors: still `RETRYABLE`, still capped at `MAX_GOAL_ATTEMPTS`. This is a regression-safety requirement.
- Migrate every existing `_NONRETRYABLE_ERROR_MARKERS` substring into the classifier so nothing currently treated as nonretryable regresses to retryable.

---

## Behavior (BDD) — Gherkin

Create `tests/services/orchestrator/features/error_classification.feature`:

```gherkin
@mocked
Feature: Error classification gates retry vs. terminal handling
  As the orchestrator
  I want to classify a failed goal's error string into a precise class
  So that environmental/terminal failures are not reflect-retried to exhaustion
  while genuinely transient and unknown failures keep their normal retry budget.

  Background:
    Given the error classifier is available

  Scenario Outline: An error string maps to a class and a retry decision
    When I classify the error "<error>"
    Then the error class is "<error_class>"
    And the retry decision is "<decision>"

    Examples: terminal dependency (missing tool / sandbox / docker)
      | error                                            | error_class         | decision |
      | SkillUnavailable: code-sandbox not available     | TERMINAL_DEPENDENCY | terminal |
      | docker: command not found                        | TERMINAL_DEPENDENCY | terminal |
      | unshare: operation not permitted (EPERM)         | TERMINAL_DEPENDENCY | terminal |
      | No such file or directory: bwrap                 | TERMINAL_DEPENDENCY | terminal |
      | ENOENT: missing executable nsjail                | TERMINAL_DEPENDENCY | terminal |
      | the requested skill is unavailable               | TERMINAL_DEPENDENCY | terminal |

    Examples: terminal credential (auth / api key)
      | error                                            | error_class         | decision |
      | Missing API key for Figma                        | TERMINAL_CREDENTIAL | terminal |
      | invalid API_KEY supplied                         | TERMINAL_CREDENTIAL | terminal |
      | 401 Unauthorized                                 | TERMINAL_CREDENTIAL | terminal |
      | 403 Forbidden: bad credential                    | TERMINAL_CREDENTIAL | terminal |
      | authentication failed                            | TERMINAL_CREDENTIAL | terminal |

    Examples: terminal network (connection / DNS)
      | error                                            | error_class         | decision |
      | Connection refused                               | TERMINAL_NETWORK    | terminal |
      | ECONNREFUSED 127.0.0.1:8080                      | TERMINAL_NETWORK    | terminal |
      | Temporary failure in name resolution (DNS)       | TERMINAL_NETWORK    | terminal |
      | Network is unreachable                           | TERMINAL_NETWORK    | terminal |
      | getaddrinfo ENOTFOUND searxng.local              | TERMINAL_NETWORK    | terminal |

    Examples: rate limited (429 — bounded backoff)
      | error                                            | error_class         | decision   |
      | HTTP 429 Too Many Requests                       | RATE_LIMITED        | backoff    |
      | Semantic Scholar rate limit exceeded             | RATE_LIMITED        | backoff    |
      | You have hit the rate-limit, retry later         | RATE_LIMITED        | backoff    |

    Examples: transient (timeouts — limited retry)
      | error                                            | error_class         | decision |
      | Request timed out after 60s                      | TRANSIENT           | retry    |
      | read timeout                                     | TRANSIENT           | retry    |
      | the operation TIMED OUT                          | TRANSIENT           | retry    |

    Examples: retryable / unknown (default — normal MAX_GOAL_ATTEMPTS)
      | error                                            | error_class         | decision |
      | assertion failed: expected 4 got 5              | RETRYABLE           | retry    |
      | the function returned the wrong value            | RETRYABLE           | retry    |
      | KeyError: 'name'                                 | RETRYABLE           | retry    |
      |                                                  | RETRYABLE           | retry    |

  Scenario: A terminal-class failure is NOT reflect-retried in the graph
    Given a goal that fails with "docker: command not found"
    When the execute node processes the failed result
    Then the goal is marked exhausted at MAX_GOAL_ATTEMPTS
    And the goal's error_class is "TERMINAL_DEPENDENCY"

  Scenario: An unknown failure is still retried up to MAX_GOAL_ATTEMPTS
    Given a goal that fails with "assertion failed: expected 4 got 5"
    When the execute node processes the failed result
    Then the goal's attempts increments by one
    And the goal's error_class is "RETRYABLE"
```

Notes on the decision column:
- `terminal` ⇒ classifier-driven exhaustion (`attempts = MAX_GOAL_ATTEMPTS`), router routes to finalize (no reflect).
- `backoff` ⇒ `RATE_LIMITED`: capped retries (`MAX_RATE_LIMIT_RETRIES`) with a sleep before re-execute; treated as retryable until the cap, then terminal.
- `retry` ⇒ normal increment, retryable up to `MAX_GOAL_ATTEMPTS`.

---

## File Map

| Path | Responsibility | Create / Modify |
|------|----------------|-----------------|
| `services/orchestrator/error_classifier.py` | `ErrorClass` enum, ordered pattern tables, `classify_error()`, `is_terminal()`, `retry_decision()` helpers. Pure module. | **Create** |
| `services/orchestrator/types.py` | Add additive `error_class: str` field to `State`. | **Modify** (`State` TypedDict, after `error` field) |
| `services/orchestrator/graph.py` | Replace `_NONRETRYABLE_ERROR_MARKERS` / `_is_nonretryable_error` with classifier calls; add `MAX_RATE_LIMIT_RETRIES`, `RATE_LIMIT_BACKOFF_SECONDS` knobs; set `error_class` in execute node; keep router behavior. | **Modify** (lines ~68-101 markers block; execute_node ~236-251; router ~635) |
| `tests/services/orchestrator/test_error_classifier.py` | Exhaustive unit tests of the pure classifier. | **Create** |
| `tests/services/orchestrator/features/error_classification.feature` | Gherkin contract (above). | **Create** |
| `tests/services/orchestrator/test_error_classification_bdd.py` | pytest-bdd step defs binding the feature. | **Create** |

---

## Task 1: The `ErrorClass` enum and pure `classify_error()`

**Files:**
- Create: `services/orchestrator/error_classifier.py`
- Test: `tests/services/orchestrator/test_error_classifier.py`

**Interfaces:**
- Produces:
  - `class ErrorClass(str, Enum)` with members `TERMINAL_DEPENDENCY`, `TERMINAL_CREDENTIAL`, `TERMINAL_NETWORK`, `RATE_LIMITED`, `TRANSIENT`, `RETRYABLE`. Each `.value` equals its name (e.g. `ErrorClass.RETRYABLE.value == "RETRYABLE"`).
  - `def classify_error(error: str | Exception | None) -> ErrorClass`
  - `TERMINAL_CLASSES: frozenset[ErrorClass]` = `{TERMINAL_DEPENDENCY, TERMINAL_CREDENTIAL, TERMINAL_NETWORK}`
  - `def is_terminal(cls: ErrorClass) -> bool` — `cls in TERMINAL_CLASSES`

- [ ] **Step 1: Write the failing unit test**

Create `tests/services/orchestrator/test_error_classifier.py`:

```python
from __future__ import annotations

import pytest

from services.orchestrator.error_classifier import (
    ErrorClass,
    classify_error,
    is_terminal,
    TERMINAL_CLASSES,
)


@pytest.mark.mocked
class TestErrorClassEnum:
    def test_members_are_name_valued_strings(self):
        assert ErrorClass.RETRYABLE.value == "RETRYABLE"
        assert ErrorClass.TERMINAL_DEPENDENCY.value == "TERMINAL_DEPENDENCY"
        # str-Enum: equals its own value
        assert ErrorClass.RATE_LIMITED == "RATE_LIMITED"

    def test_terminal_classes_set(self):
        assert TERMINAL_CLASSES == frozenset(
            {
                ErrorClass.TERMINAL_DEPENDENCY,
                ErrorClass.TERMINAL_CREDENTIAL,
                ErrorClass.TERMINAL_NETWORK,
            }
        )
        assert is_terminal(ErrorClass.TERMINAL_NETWORK) is True
        assert is_terminal(ErrorClass.RETRYABLE) is False
        assert is_terminal(ErrorClass.RATE_LIMITED) is False


@pytest.mark.mocked
class TestClassifyDependency:
    @pytest.mark.parametrize(
        "err",
        [
            "SkillUnavailable: code-sandbox not available",
            "docker: command not found",
            "unshare: operation not permitted (EPERM)",
            "No such file or directory: bwrap",
            "ENOENT: missing executable nsjail",
            "the requested skill is unavailable",
            "permission denied running container",
        ],
    )
    def test_dependency_terminal(self, err):
        assert classify_error(err) == ErrorClass.TERMINAL_DEPENDENCY


@pytest.mark.mocked
class TestClassifyCredential:
    @pytest.mark.parametrize(
        "err",
        [
            "Missing API key for Figma",
            "invalid API_KEY supplied",
            "401 Unauthorized",
            "403 Forbidden: bad credential",
            "authentication failed",
            "apikey rejected",
        ],
    )
    def test_credential_terminal(self, err):
        assert classify_error(err) == ErrorClass.TERMINAL_CREDENTIAL


@pytest.mark.mocked
class TestClassifyNetwork:
    @pytest.mark.parametrize(
        "err",
        [
            "Connection refused",
            "ECONNREFUSED 127.0.0.1:8080",
            "Temporary failure in name resolution (DNS)",
            "Network is unreachable",
            "getaddrinfo ENOTFOUND searxng.local",
        ],
    )
    def test_network_terminal(self, err):
        assert classify_error(err) == ErrorClass.TERMINAL_NETWORK


@pytest.mark.mocked
class TestClassifyRateLimited:
    @pytest.mark.parametrize(
        "err",
        [
            "HTTP 429 Too Many Requests",
            "Semantic Scholar rate limit exceeded",
            "You have hit the rate-limit, retry later",
        ],
    )
    def test_rate_limited(self, err):
        assert classify_error(err) == ErrorClass.RATE_LIMITED


@pytest.mark.mocked
class TestClassifyTransient:
    @pytest.mark.parametrize(
        "err",
        [
            "Request timed out after 60s",
            "read timeout",
            "the operation TIMED OUT",
        ],
    )
    def test_transient(self, err):
        assert classify_error(err) == ErrorClass.TRANSIENT


@pytest.mark.mocked
class TestClassifyRetryableDefault:
    @pytest.mark.parametrize(
        "err",
        [
            "assertion failed: expected 4 got 5",
            "the function returned the wrong value",
            "KeyError: 'name'",
            "",
            None,
        ],
    )
    def test_unknown_is_retryable(self, err):
        assert classify_error(err) == ErrorClass.RETRYABLE


@pytest.mark.mocked
class TestClassifierProperties:
    def test_case_insensitive(self):
        assert classify_error("DOCKER: COMMAND NOT FOUND") == ErrorClass.TERMINAL_DEPENDENCY
        assert classify_error("connection REFUSED") == ErrorClass.TERMINAL_NETWORK
        assert classify_error("API_KEY MISSING") == ErrorClass.TERMINAL_CREDENTIAL

    def test_deterministic(self):
        s = "Connection refused while reaching searxng"
        assert classify_error(s) == classify_error(s) == ErrorClass.TERMINAL_NETWORK

    def test_accepts_exception_instance(self):
        assert classify_error(ConnectionRefusedError("Connection refused")) == ErrorClass.TERMINAL_NETWORK
        assert classify_error(RuntimeError("docker not found")) == ErrorClass.TERMINAL_DEPENDENCY

    def test_credential_beats_network_when_both_present(self):
        # "401 Unauthorized" returned by a remote host over a working connection
        # is a credential problem, not a network one. Credential is checked first.
        assert classify_error("401 Unauthorized from api.semanticscholar.org") == ErrorClass.TERMINAL_CREDENTIAL

    def test_rate_limit_beats_network_429_noise(self):
        # A 429 is rate limiting even if the message also mentions the host/connection.
        assert classify_error("429 Too Many Requests from connection pool") == ErrorClass.RATE_LIMITED

    def test_migrated_markers_still_terminal(self):
        # Every legacy _NONRETRYABLE_ERROR_MARKERS substring must still classify as
        # a terminal OR rate-limited class (i.e. NOT plain RETRYABLE), so nothing
        # previously caught as nonretryable regresses.
        legacy = [
            "skillunavailable", "not available", "unavailable", "no such",
            "not found", "missing", "docker", "permission denied", "eperm",
            "enoent", "connection refused", "network", "timed out", "timeout",
            "api key", "apikey", "credential", "rate limit", "429",
        ]
        for marker in legacy:
            cls = classify_error(marker)
            assert cls != ErrorClass.RETRYABLE, f"{marker!r} regressed to RETRYABLE"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_error_classifier.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.orchestrator.error_classifier'`

- [ ] **Step 3: Write the classifier module**

Create `services/orchestrator/error_classifier.py`:

```python
# services/orchestrator/error_classifier.py
"""Pure, deterministic, case-insensitive classification of failure strings.

Replaces graph.py's crude _NONRETRYABLE_ERROR_MARKERS substring tuple. On a
single-GPU pod most skill failures are ENVIRONMENTAL/terminal (no Docker, no
API key, no network) and reflect-retrying them to exhaustion is the documented
#1 latency sink. The orchestrator uses the returned ErrorClass to decide
whether to reflect-retry, back off, or finalize immediately.

This module is PURE: no I/O, no env reads, no logging. classify_error() is a
total function over (str | Exception | None).
"""
from __future__ import annotations

from enum import Enum


class ErrorClass(str, Enum):
    """Mutually-exclusive failure categories. .value == .name for JSON safety."""
    TERMINAL_DEPENDENCY = "TERMINAL_DEPENDENCY"   # missing tool/sandbox/skill/docker
    TERMINAL_CREDENTIAL = "TERMINAL_CREDENTIAL"   # missing/invalid api key or auth
    TERMINAL_NETWORK = "TERMINAL_NETWORK"         # connection refused / DNS / unreachable
    RATE_LIMITED = "RATE_LIMITED"                 # 429 — bounded backoff then terminal
    TRANSIENT = "TRANSIENT"                       # timeouts — limited retry
    RETRYABLE = "RETRYABLE"                       # default / unknown — normal retry budget


TERMINAL_CLASSES: frozenset[ErrorClass] = frozenset(
    {
        ErrorClass.TERMINAL_DEPENDENCY,
        ErrorClass.TERMINAL_CREDENTIAL,
        ErrorClass.TERMINAL_NETWORK,
    }
)

# Ordered list of (ErrorClass, substrings). ORDER MATTERS: the first class with
# any matching substring wins. Order encodes precedence decisions:
#   1. RATE_LIMITED first — a 429 is rate limiting even if it mentions the host
#      ("429 ... from connection pool"); it must not be swallowed by NETWORK.
#   2. TERMINAL_CREDENTIAL before TERMINAL_NETWORK — a 401/403 over a working
#      connection is an auth problem, not a network one.
#   3. TERMINAL_NETWORK before TRANSIENT — "connection refused" is terminal even
#      though it is networky; only pure timeouts are TRANSIENT.
#   4. TERMINAL_DEPENDENCY catches missing tools/skills/sandbox.
#   5. TRANSIENT for bare timeouts.
# Every substring is lower-case; matching is done against the lower-cased input.
# All legacy _NONRETRYABLE_ERROR_MARKERS substrings are preserved below.
_PATTERNS: tuple[tuple[ErrorClass, tuple[str, ...]], ...] = (
    (
        ErrorClass.RATE_LIMITED,
        ("429", "rate limit", "rate-limit", "too many requests"),
    ),
    (
        ErrorClass.TERMINAL_CREDENTIAL,
        (
            "api key", "api_key", "apikey", "credential",
            "unauthorized", "401", "403", "forbidden",
            "authentication failed", "auth failed", "permission denied",
        ),
    ),
    (
        ErrorClass.TERMINAL_NETWORK,
        (
            "connection refused", "econnrefused", "network is unreachable",
            "name resolution", "enotfound", "getaddrinfo", "network",
            "dns",
        ),
    ),
    (
        ErrorClass.TERMINAL_DEPENDENCY,
        (
            "skillunavailable", "unavailable", "not available", "no such",
            "not found", "missing", "docker", "unshare", "eperm", "enoent",
            "nsjail", "bwrap", "gvisor", "sandbox",
        ),
    ),
    (
        ErrorClass.TRANSIENT,
        ("timed out", "timeout"),
    ),
)


def classify_error(error: "str | Exception | None") -> ErrorClass:
    """Classify a failure into exactly one ErrorClass.

    Pure, case-insensitive, deterministic, total. Unknown/empty -> RETRYABLE
    (preserving the legacy default: retry up to MAX_GOAL_ATTEMPTS).
    """
    if error is None:
        return ErrorClass.RETRYABLE
    text = str(error).strip()
    if not text:
        return ErrorClass.RETRYABLE
    low = text.lower()
    for cls, substrings in _PATTERNS:
        if any(s in low for s in substrings):
            return cls
    return ErrorClass.RETRYABLE


def is_terminal(cls: ErrorClass) -> bool:
    """True for the three terminal classes (never reflect-retry these)."""
    return cls in TERMINAL_CLASSES
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_error_classifier.py -q`
Expected: PASS — all parametrized cases green.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/error_classifier.py tests/services/orchestrator/test_error_classifier.py
git commit -m "feat(orchestrator): pure error classifier (ErrorClass + classify_error)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Add `error_class` field to `State`

**Files:**
- Modify: `services/orchestrator/types.py` (the `State` TypedDict, after the `error` field at line ~49)
- Test: `tests/services/orchestrator/test_error_classifier.py` (append a small import/contract test)

**Interfaces:**
- Consumes: `ErrorClass` (Task 1).
- Produces: `State["error_class"]` (a `str` holding an `ErrorClass.value`).

- [ ] **Step 1: Write the failing test**

Append to `tests/services/orchestrator/test_error_classifier.py`:

```python
@pytest.mark.mocked
class TestStateField:
    def test_state_accepts_error_class_field(self):
        from services.orchestrator.types import State  # noqa: F401
        # TypedDict total=False: error_class is an optional key. A plain dict
        # carrying it must satisfy the annotation at type-check time and at
        # runtime round-trip cleanly.
        s: State = {"error_class": ErrorClass.TERMINAL_DEPENDENCY.value}
        assert s["error_class"] == "TERMINAL_DEPENDENCY"

    def test_error_class_annotation_present(self):
        from services.orchestrator.types import State
        assert "error_class" in State.__annotations__
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_error_classifier.py::TestStateField -q`
Expected: FAIL — `test_error_class_annotation_present` asserts `"error_class" in State.__annotations__` → `AssertionError`.

- [ ] **Step 3: Add the field**

In `services/orchestrator/types.py`, inside the `State` TypedDict, immediately after the line `    error: str | None` (line ~49), add:

```python
    error_class: str                  # ErrorClass.value of the last failed goal (observability + routing)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_error_classifier.py::TestStateField -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/types.py tests/services/orchestrator/test_error_classifier.py
git commit -m "feat(orchestrator): add additive State.error_class field

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Wire the classifier into `execute_node` (replace the substring test)

**Files:**
- Modify: `services/orchestrator/graph.py`
  - Remove `_NONRETRYABLE_ERROR_MARKERS` (lines ~71-91) and `_is_nonretryable_error` (lines ~94-101).
  - Add knobs `MAX_RATE_LIMIT_RETRIES`, `RATE_LIMIT_BACKOFF_SECONDS` near the other knobs (after line ~66).
  - Rewrite the failure branch in `execute_node` (lines ~236-262) to classify and decide.
- Test: `tests/services/orchestrator/test_graph.py` (append `TestExecuteNodeClassification`)

**Interfaces:**
- Consumes: `classify_error`, `ErrorClass`, `is_terminal` (Task 1); `State["error_class"]` (Task 2).
- Produces: `execute_node` now writes `tree[gid]["attempts"]` and a top-level `"error_class"` in its returned delta. For a terminal class: `attempts = MAX_GOAL_ATTEMPTS`. For `RATE_LIMITED`: `attempts += 1`, capped exhausted once `attempts >= MAX_RATE_LIMIT_RETRIES` (whichever cap is smaller wins via `min`). For `TRANSIENT`/`RETRYABLE`: `attempts += 1`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/services/orchestrator/test_graph.py`:

```python
@pytest.mark.mocked
class TestExecuteNodeClassification:
    @pytest.mark.asyncio
    async def test_terminal_dependency_exhausts_immediately(self):
        """A no-Docker / missing-tool failure is marked exhausted at MAX_GOAL_ATTEMPTS
        in ONE pass (no reflect-retry), and records error_class."""
        from services.orchestrator.graph import make_nodes, MAX_GOAL_ATTEMPTS
        from services.orchestrator.coding_orchestrator import (
            CodingOrchestrator, AsyncOrchestrator, Result,
        )

        mock_orch = MagicMock(spec=CodingOrchestrator)
        result = Result(id="root", summary="docker: command not found", ok=False)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)
        mock_async_orch.plan_and_dispatch = AsyncMock(return_value=[result])

        _, execute_node, *_ = make_nodes(mock_orch, mock_async_orch)
        state = _make_state()
        delta = await execute_node(state)

        assert delta["goal_tree"]["root"]["attempts"] == MAX_GOAL_ATTEMPTS
        assert delta["goal_tree"]["root"]["status"] == Status.FAILED.value
        assert delta["error_class"] == "TERMINAL_DEPENDENCY"

    @pytest.mark.asyncio
    async def test_terminal_credential_exhausts_immediately(self):
        from services.orchestrator.graph import make_nodes, MAX_GOAL_ATTEMPTS
        from services.orchestrator.coding_orchestrator import (
            CodingOrchestrator, AsyncOrchestrator, Result,
        )

        mock_orch = MagicMock(spec=CodingOrchestrator)
        result = Result(id="root", summary="Missing API key for Figma", ok=False)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)
        mock_async_orch.plan_and_dispatch = AsyncMock(return_value=[result])

        _, execute_node, *_ = make_nodes(mock_orch, mock_async_orch)
        delta = await execute_node(_make_state())
        assert delta["goal_tree"]["root"]["attempts"] == MAX_GOAL_ATTEMPTS
        assert delta["error_class"] == "TERMINAL_CREDENTIAL"

    @pytest.mark.asyncio
    async def test_unknown_error_increments_by_one(self):
        """Regression-safety: an unknown failure stays RETRYABLE and increments
        attempts by exactly one (today's behavior)."""
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import (
            CodingOrchestrator, AsyncOrchestrator, Result,
        )

        mock_orch = MagicMock(spec=CodingOrchestrator)
        result = Result(id="root", summary="assertion failed: expected 4 got 5", ok=False)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)
        mock_async_orch.plan_and_dispatch = AsyncMock(return_value=[result])

        _, execute_node, *_ = make_nodes(mock_orch, mock_async_orch)
        state = _make_state()
        before = state["goal_tree"]["root"].get("attempts", 0)
        delta = await execute_node(state)
        assert delta["goal_tree"]["root"]["attempts"] == before + 1
        assert delta["error_class"] == "RETRYABLE"

    @pytest.mark.asyncio
    async def test_transient_timeout_increments_by_one(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import (
            CodingOrchestrator, AsyncOrchestrator, Result,
        )

        mock_orch = MagicMock(spec=CodingOrchestrator)
        result = Result(id="root", summary="Request timed out after 60s", ok=False)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)
        mock_async_orch.plan_and_dispatch = AsyncMock(return_value=[result])

        _, execute_node, *_ = make_nodes(mock_orch, mock_async_orch)
        state = _make_state()
        before = state["goal_tree"]["root"].get("attempts", 0)
        delta = await execute_node(state)
        assert delta["goal_tree"]["root"]["attempts"] == before + 1
        assert delta["error_class"] == "TRANSIENT"

    @pytest.mark.asyncio
    async def test_rate_limited_capped_by_max_rate_limit_retries(self, monkeypatch):
        """RATE_LIMITED retries up to MAX_RATE_LIMIT_RETRIES, then is exhausted."""
        import services.orchestrator.graph as g
        from services.orchestrator.coding_orchestrator import (
            CodingOrchestrator, AsyncOrchestrator, Result,
        )

        monkeypatch.setattr(g, "MAX_RATE_LIMIT_RETRIES", 1)
        monkeypatch.setattr(g, "RATE_LIMIT_BACKOFF_SECONDS", 0.0)

        mock_orch = MagicMock(spec=CodingOrchestrator)
        result = Result(id="root", summary="HTTP 429 Too Many Requests", ok=False)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)
        mock_async_orch.plan_and_dispatch = AsyncMock(return_value=[result])

        _, execute_node, *_ = g.make_nodes(mock_orch, mock_async_orch)
        state = _make_state()
        # Pretend we've already retried once: at the cap, this pass exhausts it.
        state["goal_tree"]["root"]["attempts"] = 1
        delta = await execute_node(state)
        assert delta["error_class"] == "RATE_LIMITED"
        assert delta["goal_tree"]["root"]["attempts"] >= g.MAX_GOAL_ATTEMPTS
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_graph.py::TestExecuteNodeClassification -q`
Expected: FAIL — `KeyError: 'error_class'` (delta has no `error_class` yet) and the terminal-dependency case currently increments by 1 (legacy substring matched "not found" → exhausts, but no `error_class` key) → `KeyError`.

- [ ] **Step 3: Edit `graph.py` — remove the legacy markers and add knobs**

Delete the entire block from line ~68 through ~101 in `services/orchestrator/graph.py`:

```python
# FIX 9: substrings (case-insensitive) that mark a failure as deterministic / environmental,
# ... (the whole _NONRETRYABLE_ERROR_MARKERS tuple) ...
def _is_nonretryable_error(err: str) -> bool:
    ...
    return any(marker in low for marker in _NONRETRYABLE_ERROR_MARKERS)
```

Replace it with the import + new knobs:

```python
from .error_classifier import classify_error, ErrorClass, is_terminal

# RATE_LIMITED handling: a 429 may be retried a bounded number of times with a
# short backoff before being treated as exhausted. Keep these small — on the
# local pod a sustained 429 (e.g. Semantic Scholar without a key) will recur.
MAX_RATE_LIMIT_RETRIES = int(os.getenv("MAX_RATE_LIMIT_RETRIES", "1"))
RATE_LIMIT_BACKOFF_SECONDS = float(os.getenv("RATE_LIMIT_BACKOFF_SECONDS", "2.0"))
```

(The `from .error_classifier import ...` line may also be placed with the other relative imports near the top of the file at line ~13; placing it beside the knobs is acceptable since Python hoists module-level imports at runtime regardless of position.)

- [ ] **Step 4: Edit `graph.py` — rewrite the `execute_node` failure branch**

In `execute_node`, replace the failure-handling block (currently lines ~226-262, the `results` loop). The full replacement for the loop body and return:

```python
        results = await async_orch.plan_and_dispatch(ready)
        last_artifact = {"type": "other", "payload": ""}
        error_class_seen: str | None = None
        for r in results:
            gid = r.id
            # Idempotency guard (FIX #4): mark per-GOAL-ID only when COMPLETED.
            if markers.get(gid) == "completed":
                continue
            new_status = Status.COMPLETED if r.ok else Status.FAILED
            if not r.ok:
                # Classify the failure to decide retry vs. terminal. Replaces the
                # crude _NONRETRYABLE_ERROR_MARKERS substring test (FIX 9).
                cls = classify_error(r.summary or "")
                error_class_seen = cls.value
                tree[gid]["error_class"] = cls.value
                if is_terminal(cls):
                    # Environmental/deterministic: will fail identically on every
                    # retry -> mark EXHAUSTED so check()/router() finalize with the
                    # honest error instead of paying reflect()+re-execute per attempt.
                    tree[gid]["attempts"] = MAX_GOAL_ATTEMPTS
                elif cls == ErrorClass.RATE_LIMITED:
                    # Bounded backoff + capped retries. Cap is the SMALLER of the
                    # rate-limit cap and the goal cap so a 429 never out-retries a
                    # normal failure.
                    prior = tree[gid].get("attempts", 0)
                    rl_cap = min(MAX_RATE_LIMIT_RETRIES, MAX_GOAL_ATTEMPTS)
                    if prior >= rl_cap:
                        tree[gid]["attempts"] = MAX_GOAL_ATTEMPTS  # exhausted
                    else:
                        if RATE_LIMIT_BACKOFF_SECONDS > 0:
                            import asyncio
                            await asyncio.sleep(RATE_LIMIT_BACKOFF_SECONDS)
                        tree[gid]["attempts"] = prior + 1
                else:
                    # TRANSIENT or RETRYABLE (unknown) -> normal increment.
                    tree[gid]["attempts"] = tree[gid].get("attempts", 0) + 1
            if r.ok:
                update_status(tree, gid, new_status, result=r.summary)
            else:
                update_status(tree, gid, new_status, result=r.summary, error=r.summary)
            if r.ok:
                markers[gid] = "completed"
            if r.ok and r.summary:
                last_artifact = {
                    "type": classify_artifact(r.summary),
                    "payload": r.summary,
                }

        out: dict = {
            "goal_tree": tree,
            "step_markers": markers,
            "last_artifact": last_artifact,
        }
        if error_class_seen is not None:
            out["error_class"] = error_class_seen
        return out
```

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_graph.py::TestExecuteNodeClassification -q`
Expected: PASS — all 5 classification cases green.

- [ ] **Step 6: Run the full graph suite to confirm no regression**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_graph.py -q`
Expected: PASS — including the pre-existing `test_e2e_subtask_exhausted_attempts_finalizes_failed` (the failure summary `"error: always fails"` classifies as `RETRYABLE`, so it still increments by one per pass to `MAX_GOAL_ATTEMPTS`).

- [ ] **Step 7: Commit**

```bash
git add services/orchestrator/graph.py tests/services/orchestrator/test_graph.py
git commit -m "feat(orchestrator): classify failures in execute_node, drop substring markers

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: pytest-bdd contract (feature + step defs)

**Files:**
- Create: `tests/services/orchestrator/features/error_classification.feature` (the full Gherkin from the "Behavior (BDD)" section above — copy it verbatim).
- Create: `tests/services/orchestrator/test_error_classification_bdd.py`

**Interfaces:**
- Consumes: `classify_error`, `ErrorClass`, `is_terminal` (Task 1); `make_nodes`, `MAX_GOAL_ATTEMPTS` (Task 3); `_make_state`-equivalent local helper.
- Produces: nothing imported by other tasks; this is a leaf verification task.

- [ ] **Step 1: Create the feature file**

Create `tests/services/orchestrator/features/error_classification.feature` containing exactly the Gherkin in the "Behavior (BDD) — Gherkin" section above (the `@mocked Feature: ...` block including the Scenario Outline, all Examples tables, and the two trailing graph scenarios).

- [ ] **Step 2: Write the step defs (failing — no impl binding yet means the file must import cleanly first)**

Create `tests/services/orchestrator/test_error_classification_bdd.py`:

```python
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.error_classifier import (
    ErrorClass,
    classify_error,
    is_terminal,
)
from services.orchestrator.types import (
    Status, create_goal, update_status,
)

pytestmark = pytest.mark.mocked

# Bind every scenario in the feature file.
scenarios("features/error_classification.feature")


def _decision_for(cls: ErrorClass) -> str:
    """Map a class to the contract's decision token used in the Examples table."""
    if is_terminal(cls):
        return "terminal"
    if cls == ErrorClass.RATE_LIMITED:
        return "backoff"
    return "retry"  # TRANSIENT or RETRYABLE


def _fresh_state() -> dict:
    tree: dict = {}
    create_goal(tree, "root", None, "top-level task")
    return {
        "session_id": "bdd-001",
        "goal_tree": tree,
        "current_goal_id": "root",
        "step_markers": {},
        "messages": [],
        "error": None,
        "root_goal": "top-level task",
        "last_artifact": {"type": "other", "payload": ""},
    }


# --- Outline scenario steps -------------------------------------------------

@pytest.fixture
def ctx() -> dict:
    return {}


@given("the error classifier is available")
def classifier_available():
    assert classify_error is not None


@when(parsers.parse('I classify the error "{error}"'))
def classify(ctx, error):
    # The Examples table renders an empty cell as the empty string.
    ctx["cls"] = classify_error(error)


@then(parsers.parse('the error class is "{error_class}"'))
def assert_class(ctx, error_class):
    assert ctx["cls"].value == error_class


@then(parsers.parse('the retry decision is "{decision}"'))
def assert_decision(ctx, decision):
    assert _decision_for(ctx["cls"]) == decision


# --- Graph scenario steps ---------------------------------------------------

@given(parsers.parse('a goal that fails with "{error}"'))
def goal_fails(ctx, error):
    from services.orchestrator.coding_orchestrator import (
        CodingOrchestrator, AsyncOrchestrator, Result,
    )
    mock_orch = MagicMock(spec=CodingOrchestrator)
    mock_async = MagicMock(spec=AsyncOrchestrator)
    mock_async.plan_and_dispatch = AsyncMock(
        return_value=[Result(id="root", summary=error, ok=False)]
    )
    ctx["orch"] = mock_orch
    ctx["async_orch"] = mock_async
    ctx["state"] = _fresh_state()
    ctx["attempts_before"] = ctx["state"]["goal_tree"]["root"].get("attempts", 0)


@when("the execute node processes the failed result")
def run_execute(ctx):
    import asyncio
    from services.orchestrator.graph import make_nodes
    _, execute_node, *_ = make_nodes(ctx["orch"], ctx["async_orch"])
    ctx["delta"] = asyncio.run(execute_node(ctx["state"]))


@then("the goal is marked exhausted at MAX_GOAL_ATTEMPTS")
def assert_exhausted(ctx):
    from services.orchestrator.graph import MAX_GOAL_ATTEMPTS
    assert ctx["delta"]["goal_tree"]["root"]["attempts"] == MAX_GOAL_ATTEMPTS


@then("the goal's attempts increments by one")
def assert_incremented(ctx):
    assert ctx["delta"]["goal_tree"]["root"]["attempts"] == ctx["attempts_before"] + 1


@then(parsers.parse('the goal\'s error_class is "{error_class}"'))
def assert_goal_error_class(ctx, error_class):
    assert ctx["delta"]["error_class"] == error_class
    assert ctx["delta"]["goal_tree"]["root"]["error_class"] == error_class
```

- [ ] **Step 3: Run the BDD suite to verify it passes**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_error_classification_bdd.py -q`
Expected: PASS — every Examples row and both graph scenarios bind and assert green. (If `pytest_bdd` import fails, the foundation plan's pytest-bdd dependency is not installed — install it before proceeding; this plan assumes it is present.)

- [ ] **Step 4: Run the whole orchestrator test dir for a final regression sweep**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/ -q`
Expected: PASS — no regressions across `test_graph.py`, `test_error_classifier.py`, and the new BDD file.

- [ ] **Step 5: Commit**

```bash
git add tests/services/orchestrator/features/error_classification.feature tests/services/orchestrator/test_error_classification_bdd.py
git commit -m "test(orchestrator): pytest-bdd contract for error classification

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage**

| Spec requirement | Task |
|------------------|------|
| New module `services/orchestrator/error_classifier.py` modeled on hermes | Task 1 |
| `classify_error(error: str \| Exception) -> ErrorClass` | Task 1 (also accepts `None`) |
| Enum: `TERMINAL_DEPENDENCY`, `TERMINAL_CREDENTIAL`, `TERMINAL_NETWORK`, `RATE_LIMITED`, `TRANSIENT`, `RETRYABLE` | Task 1 |
| Network is TERMINAL (decision recorded: connection-refused/DNS are terminal; only pure timeouts are TRANSIENT) | Task 1 (`TERMINAL_NETWORK` vs `TRANSIENT` ordering + comments) |
| Pure, case-insensitive, deterministic, exhaustively tested (caps, `API_KEY`, `EPERM`, `429`, `Connection refused`, `unshare`) | Task 1 tests (`TestClassifierProperties`, parametrized cases all present) |
| Preserve unknown → RETRYABLE up to MAX_GOAL_ATTEMPTS (regression-safe) | Task 1 (`test_unknown_is_retryable`), Task 3 (`test_unknown_error_increments_by_one`, full-suite e2e regression) |
| Migrate every legacy `_NONRETRYABLE_ERROR_MARKERS` case | Task 1 (`test_migrated_markers_still_terminal` covers all 19 markers), Task 3 deletes the tuple |
| Additive State field `error_class` | Task 2 |
| Router/check use the class: terminal skips reflect, rate-limited backoff, transient/unknown retry | Task 3 (execute_node sets `attempts = MAX_GOAL_ATTEMPTS` for terminal; existing `router()` then routes terminal→finalize since `attempts >= MAX_GOAL_ATTEMPTS`) |
| Keep env knobs for caps | Task 3 (`MAX_RATE_LIMIT_RETRIES`, `RATE_LIMIT_BACKOFF_SECONDS`; existing `MAX_GOAL_ATTEMPTS` unchanged) |
| BDD feature `@mocked` with Scenario Outline + Examples mapping error→class AND retry/terminal decision | "Behavior" section + Task 4 |
| Step defs in `test_error_classification_bdd.py` using pytest_bdd | Task 4 |
| Unit TDD tests in `test_error_classifier.py` | Task 1, Task 2 |
| Graph wire-in proving terminal NOT retried, unknown still retried | Task 3 tests + Task 4 graph scenarios |

No gaps.

**2. Placeholder scan:** No "TBD"/"handle edge cases"/"similar to Task N". Every code step shows complete code; every run step shows an exact command and expected output. Clean.

**3. Type consistency:**
- `classify_error` / `ErrorClass` / `is_terminal` / `TERMINAL_CLASSES` names identical across Tasks 1, 3, 4.
- `ErrorClass` member names identical everywhere (`TERMINAL_DEPENDENCY`, `TERMINAL_CREDENTIAL`, `TERMINAL_NETWORK`, `RATE_LIMITED`, `TRANSIENT`, `RETRYABLE`).
- `error_class` field name consistent: `State.error_class` (Task 2), `tree[gid]["error_class"]` and delta `"error_class"` (Task 3), asserted with the same key (Task 4).
- Knobs `MAX_RATE_LIMIT_RETRIES` / `RATE_LIMIT_BACKOFF_SECONDS` defined once (Task 3) and `monkeypatch`-ed by the same names in the rate-limit test.
- The router relies on the existing invariant `attempts >= MAX_GOAL_ATTEMPTS ⇒ not reflect` (graph.py line ~635), which the execute_node terminal branch satisfies by setting `attempts = MAX_GOAL_ATTEMPTS` — no router edit needed, no signature drift.

Consistent.

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-06-25-error-classification.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
