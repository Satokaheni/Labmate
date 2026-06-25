# Model Endpoint Failover + Degraded Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a thin resilient model-client wrapper that fails over across an ordered list of llama-server base URLs on transient transport errors (conn-refused / 5xx / timeout) with bounded jittered retries, never fails over on 4xx content errors, and route the orchestrator's model calls through it.

**Architecture:** A new module `services/orchestrator/model_client.py` exposes a single coroutine `acompletion_with_failover(...)` (and a thin `ModelClient` holder) that wraps `litellm.acompletion`. It accepts an ordered list of base URLs, walks them in order, and for each url retries up to a bounded count on *retryable* exceptions with jittered backoff. Non-retryable (4xx content) errors are re-raised immediately with no failover. When all (url, attempt) combinations are exhausted it raises a terminal `AllEndpointsExhausted`. The orchestrator's `architect()`, `editor()`, `react_execute()`, `aggregate()`, and `stream_final_answer()` calls are re-pointed from `litellm.acompletion(...)` to this wrapper, passing their existing `api_base` as the primary url plus any `LABMATE_FALLBACK_BASES`.

**Tech Stack:** Python 3.11+ asyncio, litellm (already a dependency), respx (already installed, 0.23.1) for HTTP-level mocking, pytest + pytest-asyncio, pytest-bdd (added by the foundation plan).

## Global Constraints

- **Single-GPU reality:** there is normally ONE model endpoint (`GEMMA_BASE`); `QWEN_BASE` defaults to `GEMMA_BASE` (CLAUDE.md). Failover is across *replica endpoints of the same model*, not across different models. With a single endpoint configured, behavior MUST equal today's direct `litellm.acompletion` call plus a bounded retry on transient transport errors.
- **No tiktoken anywhere** (CLAUDE.md rule 3). This feature counts no tokens — do not import it.
- **stdout is sacred** — never `print()`; use the `logging` module to stderr only (CLAUDE.md rule 1).
- **asyncio-correct** — no `asyncio.run()` inside a coroutine; no blocking `time.sleep` in async code (use `await asyncio.sleep(...)`).
- **Every model call sets `thinking_budget_tokens`** in `extra_body` (CLAUDE.md rule 6). The wrapper is transparent to `extra_body` — it forwards `**kwargs` verbatim, so callers keep setting it.
- **4xx is terminal — never fail over.** Only connection-refused, timeouts, and 5xx (server/gateway) errors trigger failover. A `BadRequestError`, `AuthenticationError`, `NotFoundError`, `UnprocessableEntityError`, or `ContextWindowExceededError` is surfaced immediately.
- **Test-friendly backoff:** backoff sleeps go through an injectable `sleep` callable (default `asyncio.sleep`) and jitter through an injectable `rng` (default `random.random`). Tests pass a no-op `sleep` and a seeded/deterministic `rng`. Production uses the real ones.
- **Env knobs** (read with `os.getenv`, defaults in code):
  - `LABMATE_FALLBACK_BASES` — comma-separated extra base URLs appended after the primary (default `""` → none).
  - `LABMATE_MODEL_MAX_ATTEMPTS_PER_BASE` — bounded retry attempts per endpoint (default `2`).
  - `LABMATE_MODEL_BACKOFF_BASE_S` — base backoff seconds (default `0.5`).
  - `LABMATE_MODEL_BACKOFF_MAX_S` — backoff cap seconds (default `4.0`).

---

## Behavior (BDD) — Gherkin

Save this verbatim as `tests/services/orchestrator/features/endpoint_failover.feature`.

```gherkin
@mocked
Feature: Model endpoint failover and degraded mode
  The model client wraps litellm.acompletion with an ordered list of base URLs.
  Transient transport errors (connection refused, 5xx, timeout) trigger failover
  to the next endpoint with bounded jittered retries. 4xx content errors are
  terminal and surfaced immediately. A single endpoint with no failures behaves
  exactly like a direct litellm call.

  Background:
    Given backoff sleeping is disabled in the test harness
    And jitter is deterministic in the test harness

  Scenario: Primary endpoint is down, secondary succeeds
    Given a primary base url that always returns connection errors
    And a secondary base url that returns a valid completion
    When I request a completion with failover across both endpoints
    Then the completion succeeds with the secondary endpoint's content
    And the secondary endpoint was called after the primary

  Scenario: All endpoints down raises a terminal error after bounded attempts
    Given a primary base url that always returns 503 Service Unavailable
    And a secondary base url that always returns connection errors
    And the per-base attempt cap is 2
    When I request a completion with failover across both endpoints
    Then an AllEndpointsExhausted error is raised
    And the primary endpoint was attempted exactly 2 times
    And the secondary endpoint was attempted exactly 2 times

  Scenario: A 4xx content error is surfaced immediately with no failover
    Given a primary base url that returns 400 Bad Request
    And a secondary base url that returns a valid completion
    When I request a completion with failover across both endpoints
    Then a BadRequestError is raised
    And the secondary endpoint was never called

  Scenario: A single endpoint transient blip recovers within the retry budget
    Given a single base url that returns 503 once and then a valid completion
    And the per-base attempt cap is 2
    When I request a completion with failover across that endpoint
    Then the completion succeeds with that endpoint's content
    And the endpoint was attempted exactly 2 times
```

---

## File Map

| Path | Create/Modify | Responsibility |
|---|---|---|
| `services/orchestrator/model_client.py` | **Create** | The resilient wrapper: error classification, endpoint list resolution, bounded jittered retry/failover loop, `acompletion_with_failover`, `AllEndpointsExhausted`. |
| `services/orchestrator/coding_orchestrator.py` | **Modify** | Route `architect`, `editor`, `react_execute`, `aggregate`, `stream_final_answer` through `acompletion_with_failover`. Resolve fallback bases once in `__init__`. |
| `tests/services/orchestrator/test_model_client.py` | **Create** | respx-driven unit tests for classification, failover order, bounded attempts, terminal error, regression-equivalence, backoff injection. |
| `tests/services/orchestrator/features/endpoint_failover.feature` | **Create** | The Gherkin above. |
| `tests/services/orchestrator/test_endpoint_failover_bdd.py` | **Create** | pytest-bdd step defs binding the feature to respx-mocked endpoints. |

---

## Task 1: Error classification helper

**Files:**
- Create: `services/orchestrator/model_client.py`
- Test: `tests/services/orchestrator/test_model_client.py`

**Interfaces:**
- Consumes: `litellm` exception classes (`litellm.APIConnectionError`, `litellm.Timeout`, `litellm.InternalServerError`, `litellm.ServiceUnavailableError`, `litellm.BadGatewayError`, `litellm.RateLimitError`, `litellm.BadRequestError`, `litellm.AuthenticationError`, `litellm.NotFoundError`, `litellm.ContextWindowExceededError`).
- Produces: `is_retryable(exc: Exception) -> bool` — `True` for transient transport errors (conn-refused, timeout, 5xx, rate-limit), `False` for 4xx content errors and everything unrecognized.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/orchestrator/test_model_client.py
from __future__ import annotations

import pytest
import litellm

from services.orchestrator.model_client import is_retryable


@pytest.mark.mocked
@pytest.mark.parametrize("exc", [
    litellm.APIConnectionError(message="refused", llm_provider="openai", model="m"),
    litellm.Timeout(message="timeout", llm_provider="openai", model="m"),
    litellm.ServiceUnavailableError(message="503", llm_provider="openai", model="m"),
    litellm.InternalServerError(message="500", llm_provider="openai", model="m"),
    litellm.RateLimitError(message="429", llm_provider="openai", model="m"),
])
def test_is_retryable_true_for_transient(exc):
    assert is_retryable(exc) is True


@pytest.mark.mocked
@pytest.mark.parametrize("exc", [
    litellm.BadRequestError(message="400", llm_provider="openai", model="m"),
    litellm.AuthenticationError(message="401", llm_provider="openai", model="m"),
    litellm.NotFoundError(message="404", llm_provider="openai", model="m"),
    litellm.ContextWindowExceededError(message="ctx", llm_provider="openai", model="m"),
    ValueError("not an llm error"),
])
def test_is_retryable_false_for_4xx_and_unknown(exc):
    assert is_retryable(exc) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/orchestrator/test_model_client.py -k is_retryable -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.orchestrator.model_client'` (or `ImportError: cannot import name 'is_retryable'`).

- [ ] **Step 3: Write minimal implementation**

```python
# services/orchestrator/model_client.py
"""Resilient model client: ordered-endpoint failover over litellm.acompletion.

Single-GPU reality (CLAUDE.md): there is normally ONE model endpoint. This module
adds bounded retry on transient transport errors plus optional failover to extra
replica endpoints listed in LABMATE_FALLBACK_BASES. 4xx content errors never
trigger failover — they are surfaced immediately.
"""
from __future__ import annotations

import litellm

# Transient transport errors → retry / fail over to the next endpoint.
_RETRYABLE = (
    litellm.APIConnectionError,
    litellm.Timeout,
    litellm.InternalServerError,
    litellm.ServiceUnavailableError,
    litellm.BadGatewayError,
    litellm.RateLimitError,
)

# 4xx content errors → terminal, never fail over.
_TERMINAL = (
    litellm.BadRequestError,
    litellm.AuthenticationError,
    litellm.NotFoundError,
    litellm.PermissionDeniedError,
    litellm.UnprocessableEntityError,
    litellm.ContextWindowExceededError,
)


def is_retryable(exc: Exception) -> bool:
    """True only for transient transport errors (conn-refused, timeout, 5xx, 429).

    4xx content errors and any unrecognized exception are NOT retryable — they are
    surfaced immediately with no failover.
    """
    if isinstance(exc, _TERMINAL):
        return False
    return isinstance(exc, _RETRYABLE)
```

Note: some `_TERMINAL` subclasses (e.g. `RateLimitError` would not be one) could in theory subclass a retryable base in future litellm versions; the explicit `_TERMINAL` check first guarantees 4xx is terminal regardless of MRO.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/services/orchestrator/test_model_client.py -k is_retryable -v`
Expected: PASS (all 10 parametrized cases green).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/model_client.py tests/services/orchestrator/test_model_client.py
git commit -m "feat(orchestrator): add retryable-error classifier for model failover"
```

---

## Task 2: Endpoint list resolution

**Files:**
- Modify: `services/orchestrator/model_client.py`
- Test: `tests/services/orchestrator/test_model_client.py`

**Interfaces:**
- Consumes: `is_retryable` (Task 1).
- Produces: `resolve_bases(primary: str, fallbacks_env: str | None = None) -> list[str]` — returns `[primary, *parsed_fallbacks]` with whitespace trimmed, blanks dropped, exact duplicates removed (first occurrence wins, order preserved). When `fallbacks_env` is `None` it reads `os.getenv("LABMATE_FALLBACK_BASES", "")`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/services/orchestrator/test_model_client.py
from services.orchestrator.model_client import resolve_bases


@pytest.mark.mocked
def test_resolve_bases_single_endpoint_no_env():
    assert resolve_bases("http://a:8000/v1", "") == ["http://a:8000/v1"]


@pytest.mark.mocked
def test_resolve_bases_appends_fallbacks_and_dedupes():
    out = resolve_bases(
        "http://a:8000/v1",
        " http://b:8000/v1 , http://a:8000/v1 ,, http://c:8000/v1 ",
    )
    assert out == ["http://a:8000/v1", "http://b:8000/v1", "http://c:8000/v1"]


@pytest.mark.mocked
def test_resolve_bases_reads_env_when_arg_is_none(monkeypatch):
    monkeypatch.setenv("LABMATE_FALLBACK_BASES", "http://b:8000/v1")
    assert resolve_bases("http://a:8000/v1", None) == [
        "http://a:8000/v1",
        "http://b:8000/v1",
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/orchestrator/test_model_client.py -k resolve_bases -v`
Expected: FAIL with `ImportError: cannot import name 'resolve_bases'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to services/orchestrator/model_client.py
import os


def resolve_bases(primary: str, fallbacks_env: str | None = None) -> list[str]:
    """Build the ordered endpoint list: [primary, *fallbacks].

    fallbacks come from `fallbacks_env` (comma-separated). When None, reads
    LABMATE_FALLBACK_BASES. Blanks are dropped; exact duplicates are removed
    preserving first-seen order so the primary always leads.
    """
    if fallbacks_env is None:
        fallbacks_env = os.getenv("LABMATE_FALLBACK_BASES", "")
    raw = [primary, *(p.strip() for p in fallbacks_env.split(","))]
    out: list[str] = []
    for url in raw:
        if url and url not in out:
            out.append(url)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/services/orchestrator/test_model_client.py -k resolve_bases -v`
Expected: PASS (3 cases green).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/model_client.py tests/services/orchestrator/test_model_client.py
git commit -m "feat(orchestrator): resolve ordered model endpoint list from env"
```

---

## Task 3: Bounded jittered backoff helper

**Files:**
- Modify: `services/orchestrator/model_client.py`
- Test: `tests/services/orchestrator/test_model_client.py`

**Interfaces:**
- Produces: `backoff_delay(attempt: int, base_s: float, max_s: float, rng) -> float` — exponential backoff `base_s * 2**attempt` capped at `max_s`, multiplied by a jitter factor in `[0.5, 1.0]` derived from `rng()` (a zero-arg callable returning a float in `[0, 1)`). `attempt` is 0-based.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/services/orchestrator/test_model_client.py
from services.orchestrator.model_client import backoff_delay


@pytest.mark.mocked
def test_backoff_grows_exponentially_and_caps():
    # rng() == 1.0 -> jitter factor 1.0 (full delay), so we see the raw curve/cap.
    one = lambda: 1.0
    assert backoff_delay(0, base_s=0.5, max_s=4.0, rng=one) == pytest.approx(0.5)
    assert backoff_delay(1, base_s=0.5, max_s=4.0, rng=one) == pytest.approx(1.0)
    assert backoff_delay(2, base_s=0.5, max_s=4.0, rng=one) == pytest.approx(2.0)
    assert backoff_delay(3, base_s=0.5, max_s=4.0, rng=one) == pytest.approx(4.0)
    # attempt 4 would be 8.0 but is capped at max_s.
    assert backoff_delay(4, base_s=0.5, max_s=4.0, rng=one) == pytest.approx(4.0)


@pytest.mark.mocked
def test_backoff_applies_jitter_floor_of_half():
    # rng() == 0.0 -> jitter factor 0.5 (half delay).
    zero = lambda: 0.0
    assert backoff_delay(1, base_s=0.5, max_s=4.0, rng=zero) == pytest.approx(0.5)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/orchestrator/test_model_client.py -k backoff -v`
Expected: FAIL with `ImportError: cannot import name 'backoff_delay'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to services/orchestrator/model_client.py
def backoff_delay(attempt: int, base_s: float, max_s: float, rng) -> float:
    """Exponential backoff with a [0.5, 1.0] jitter factor.

    attempt is 0-based. Raw delay = min(base_s * 2**attempt, max_s); the returned
    delay is that raw value scaled by (0.5 + 0.5*rng()), so jitter never drops the
    delay below half the curve. rng is a zero-arg callable returning a float in [0, 1).
    """
    raw = min(base_s * (2 ** attempt), max_s)
    jitter = 0.5 + 0.5 * rng()
    return raw * jitter
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/services/orchestrator/test_model_client.py -k backoff -v`
Expected: PASS (2 cases green).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/model_client.py tests/services/orchestrator/test_model_client.py
git commit -m "feat(orchestrator): add jittered exponential backoff helper"
```

---

## Task 4: The failover loop — `acompletion_with_failover` + `AllEndpointsExhausted`

**Files:**
- Modify: `services/orchestrator/model_client.py`
- Test: `tests/services/orchestrator/test_model_client.py`

**Interfaces:**
- Consumes: `is_retryable`, `resolve_bases`, `backoff_delay` (Tasks 1–3).
- Produces:
  - `class AllEndpointsExhausted(RuntimeError)` — terminal error with `.attempts: list[tuple[str, Exception]]` (one entry per (base, exception) attempted).
  - `async def acompletion_with_failover(*, model: str, bases: list[str], api_key: str = "not-needed", max_attempts_per_base: int | None = None, base_backoff_s: float | None = None, max_backoff_s: float | None = None, sleep=asyncio.sleep, rng=random.random, _acompletion=None, **kwargs)` — walks `bases` in order; for each base, calls `litellm.acompletion(model=model, api_base=base, api_key=api_key, **kwargs)` up to `max_attempts_per_base` times, sleeping `backoff_delay(...)` between *retries on the same base*; on a retryable error advances to the next attempt/base; on a non-retryable error re-raises immediately; returns the first successful response. When all bases+attempts are exhausted on retryable errors, raises `AllEndpointsExhausted`. `_acompletion` overrides the litellm call for unit tests; defaults to `litellm.acompletion`.

Note: `**kwargs` carries `messages`, `tools`, `tool_choice`, `stream`, `extra_body` (incl. `thinking_budget_tokens`) verbatim — the wrapper is transparent to them.

- [ ] **Step 1: Write the failing test (failover + terminal + 4xx-no-failover, using an injected fake _acompletion — no HTTP needed here)**

```python
# append to tests/services/orchestrator/test_model_client.py
import asyncio
from types import SimpleNamespace

from services.orchestrator.model_client import (
    acompletion_with_failover,
    AllEndpointsExhausted,
)


def _ok(content="hi"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


async def _noop_sleep(_):  # never actually wait in tests
    return None


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_failover_primary_down_secondary_succeeds():
    calls = []

    async def fake(*, model, api_base, api_key, **kw):
        calls.append(api_base)
        if api_base == "http://a/v1":
            raise litellm.APIConnectionError(message="refused", llm_provider="openai", model=model)
        return _ok("from-b")

    r = await acompletion_with_failover(
        model="openai/gemma-4-31b",
        bases=["http://a/v1", "http://b/v1"],
        max_attempts_per_base=1,
        sleep=_noop_sleep,
        rng=lambda: 0.0,
        _acompletion=fake,
        messages=[{"role": "user", "content": "hi"}],
        extra_body={"thinking_budget_tokens": 0},
    )
    assert r.choices[0].message.content == "from-b"
    assert calls == ["http://a/v1", "http://b/v1"]


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_all_endpoints_exhausted_raises_terminal_after_bounded_attempts():
    calls = []

    async def fake(*, model, api_base, api_key, **kw):
        calls.append(api_base)
        raise litellm.ServiceUnavailableError(message="503", llm_provider="openai", model=model)

    with pytest.raises(AllEndpointsExhausted) as ei:
        await acompletion_with_failover(
            model="openai/gemma-4-31b",
            bases=["http://a/v1", "http://b/v1"],
            max_attempts_per_base=2,
            sleep=_noop_sleep,
            rng=lambda: 0.0,
            _acompletion=fake,
            messages=[{"role": "user", "content": "hi"}],
            extra_body={"thinking_budget_tokens": 0},
        )
    # 2 attempts per base × 2 bases == 4 total calls
    assert calls == ["http://a/v1", "http://a/v1", "http://b/v1", "http://b/v1"]
    assert len(ei.value.attempts) == 4


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_4xx_is_terminal_no_failover():
    calls = []

    async def fake(*, model, api_base, api_key, **kw):
        calls.append(api_base)
        raise litellm.BadRequestError(message="400", llm_provider="openai", model=model)

    with pytest.raises(litellm.BadRequestError):
        await acompletion_with_failover(
            model="openai/gemma-4-31b",
            bases=["http://a/v1", "http://b/v1"],
            max_attempts_per_base=2,
            sleep=_noop_sleep,
            rng=lambda: 0.0,
            _acompletion=fake,
            messages=[{"role": "user", "content": "hi"}],
            extra_body={"thinking_budget_tokens": 0},
        )
    assert calls == ["http://a/v1"]  # secondary never touched


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_single_endpoint_transient_blip_recovers_within_budget():
    calls = []

    async def fake(*, model, api_base, api_key, **kw):
        calls.append(api_base)
        if len(calls) == 1:
            raise litellm.ServiceUnavailableError(message="503", llm_provider="openai", model=model)
        return _ok("recovered")

    r = await acompletion_with_failover(
        model="openai/gemma-4-31b",
        bases=["http://a/v1"],
        max_attempts_per_base=2,
        sleep=_noop_sleep,
        rng=lambda: 0.0,
        _acompletion=fake,
        messages=[{"role": "user", "content": "hi"}],
        extra_body={"thinking_budget_tokens": 0},
    )
    assert r.choices[0].message.content == "recovered"
    assert calls == ["http://a/v1", "http://a/v1"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/orchestrator/test_model_client.py -k "failover or exhausted or terminal or blip" -v`
Expected: FAIL with `ImportError: cannot import name 'acompletion_with_failover'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to services/orchestrator/model_client.py
import asyncio
import logging
import random

_log = logging.getLogger("orchestrator.model_client")

_DEFAULT_MAX_ATTEMPTS = int(os.getenv("LABMATE_MODEL_MAX_ATTEMPTS_PER_BASE", "2"))
_DEFAULT_BACKOFF_BASE = float(os.getenv("LABMATE_MODEL_BACKOFF_BASE_S", "0.5"))
_DEFAULT_BACKOFF_MAX = float(os.getenv("LABMATE_MODEL_BACKOFF_MAX_S", "4.0"))


class AllEndpointsExhausted(RuntimeError):
    """All endpoints failed with retryable errors after bounded attempts."""

    def __init__(self, attempts: list[tuple[str, Exception]]) -> None:
        self.attempts = attempts
        bases = ", ".join(sorted({b for b, _ in attempts}))
        last = attempts[-1][1] if attempts else None
        super().__init__(
            f"all model endpoints exhausted after {len(attempts)} attempt(s) "
            f"across [{bases}]; last error: {type(last).__name__}: {last}"
        )


async def acompletion_with_failover(
    *,
    model: str,
    bases: list[str],
    api_key: str = "not-needed",
    max_attempts_per_base: int | None = None,
    base_backoff_s: float | None = None,
    max_backoff_s: float | None = None,
    sleep=asyncio.sleep,
    rng=random.random,
    _acompletion=None,
    **kwargs,
):
    """Call litellm.acompletion across an ordered endpoint list with failover.

    For each base in `bases`, retry up to `max_attempts_per_base` on retryable
    transport errors with jittered backoff between attempts. On a non-retryable
    (4xx) error, re-raise immediately (no failover). Returns the first success.
    Raises AllEndpointsExhausted when every base+attempt fails on retryable errors.

    `**kwargs` (messages, tools, tool_choice, stream, extra_body, ...) is forwarded
    verbatim — extra_body must already carry thinking_budget_tokens per CLAUDE.md.
    """
    if not bases:
        raise ValueError("acompletion_with_failover requires at least one base url")
    if _acompletion is None:
        _acompletion = litellm.acompletion
    attempts_cap = max_attempts_per_base if max_attempts_per_base is not None else _DEFAULT_MAX_ATTEMPTS
    bb = base_backoff_s if base_backoff_s is not None else _DEFAULT_BACKOFF_BASE
    mb = max_backoff_s if max_backoff_s is not None else _DEFAULT_BACKOFF_MAX

    history: list[tuple[str, Exception]] = []
    for base in bases:
        for attempt in range(attempts_cap):
            try:
                return await _acompletion(
                    model=model, api_base=base, api_key=api_key, **kwargs
                )
            except Exception as exc:  # noqa: BLE001 — classify, then re-raise or retry
                if not is_retryable(exc):
                    # 4xx / unknown: terminal, surface immediately, no failover.
                    raise
                history.append((base, exc))
                _log.warning(
                    "model endpoint %s attempt %d/%d failed (%s); will retry/failover",
                    base, attempt + 1, attempts_cap, type(exc).__name__,
                )
                # Sleep only if another attempt on THIS base remains.
                if attempt + 1 < attempts_cap:
                    await sleep(backoff_delay(attempt, bb, mb, rng))
    raise AllEndpointsExhausted(history)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/services/orchestrator/test_model_client.py -k "failover or exhausted or terminal or blip" -v`
Expected: PASS (4 cases green).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/model_client.py tests/services/orchestrator/test_model_client.py
git commit -m "feat(orchestrator): bounded jittered endpoint failover loop"
```

---

## Task 5: respx HTTP-level regression test — single endpoint behaves like a direct call

**Files:**
- Test: `tests/services/orchestrator/test_model_client.py`

**Interfaces:**
- Consumes: `acompletion_with_failover` (Task 4). This task uses the REAL `litellm.acompletion` (no `_acompletion` override) and mocks the HTTP layer with respx, proving the wrapper is wire-compatible with litellm's OpenAI transport.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/services/orchestrator/test_model_client.py
import respx
import httpx


@pytest.mark.mocked
@pytest.mark.asyncio
@respx.mock
async def test_single_endpoint_no_failures_matches_direct_call():
    # llama.cpp OpenAI-compatible chat completions endpoint.
    route = respx.post("http://a:8000/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "pong"},
                     "finish_reason": "stop"}
                ],
            },
        )
    )
    r = await acompletion_with_failover(
        model="openai/gemma-4-31b",
        bases=["http://a:8000/v1"],
        sleep=lambda _ : None,   # not used; one base, one success
        rng=lambda: 0.0,
        messages=[{"role": "user", "content": "ping"}],
        extra_body={"thinking_budget_tokens": 0},
    )
    assert r.choices[0].message.content == "pong"
    assert route.call_count == 1


@pytest.mark.mocked
@pytest.mark.asyncio
@respx.mock
async def test_respx_primary_503_then_secondary_200():
    respx.post("http://a:8000/v1/chat/completions").mock(
        return_value=httpx.Response(503, json={"error": {"message": "down"}})
    )
    secondary = respx.post("http://b:8000/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"index": 0,
                               "message": {"role": "assistant", "content": "from-b"},
                               "finish_reason": "stop"}]},
        )
    )

    async def _noop(_):
        return None

    r = await acompletion_with_failover(
        model="openai/gemma-4-31b",
        bases=["http://a:8000/v1", "http://b:8000/v1"],
        max_attempts_per_base=1,
        sleep=_noop,
        rng=lambda: 0.0,
        messages=[{"role": "user", "content": "hi"}],
        extra_body={"thinking_budget_tokens": 0},
    )
    assert r.choices[0].message.content == "from-b"
    assert secondary.call_count == 1
```

- [ ] **Step 2: Run test to verify it fails (or errors)**

Run: `pytest tests/services/orchestrator/test_model_client.py -k "direct_call or 503_then_secondary" -v`
Expected: PASS if Task 4 is implemented correctly. If litellm wraps the 503 in a non-retryable class on this version, the test FAILS by raising instead of failing over — that is the signal to widen `_RETRYABLE` in `model_client.py` to include the actual class litellm raises for a 503 (inspect the traceback's exception type, add it to `_RETRYABLE`, rerun). Document the observed class in a code comment.

- [ ] **Step 3: Implement (only if Step 2 surfaced a gap)**

If the 503 surfaced as e.g. `litellm.APIError` rather than `ServiceUnavailableError`, add it to `_RETRYABLE`:

```python
# services/orchestrator/model_client.py — widen _RETRYABLE if Step 2 required it
_RETRYABLE = (
    litellm.APIConnectionError,
    litellm.Timeout,
    litellm.InternalServerError,
    litellm.ServiceUnavailableError,
    litellm.BadGatewayError,
    litellm.RateLimitError,
    # Observed: litellm surfaced a bare 503 as <ClassName> on this version — see test.
)
```

If Step 2 already passed, make no change.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/services/orchestrator/test_model_client.py -v`
Expected: PASS (entire file green).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/model_client.py tests/services/orchestrator/test_model_client.py
git commit -m "test(orchestrator): respx wire-level failover + single-endpoint regression"
```

---

## Task 6: Wire the orchestrator's model calls through the wrapper

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py`
  - `AsyncOrchestrator.__init__` (around lines 107–133) — add `self._bases`.
  - `AsyncOrchestrator.react_execute` ReAct loop call (lines 415–423).
  - `AsyncOrchestrator.aggregate` call (lines 686–692).
  - `CodingOrchestrator.__init__` (around lines 712–735) — add `self._bases`.
  - `CodingOrchestrator.architect` (lines 797–804).
  - `CodingOrchestrator.editor` (lines 812–819) — note: editor uses `self._qwen_base`; build an editor-specific base list.
  - `CodingOrchestrator.stream_final_answer` (lines 901–908).
- Test: `tests/services/orchestrator/test_model_client.py` (one wiring test) + existing `tests/services/orchestrator/test_coding_orchestrator.py` must still pass.

**Interfaces:**
- Consumes: `acompletion_with_failover` (Task 4), `resolve_bases` (Task 2).
- Produces: orchestrator instances expose `self._bases` (gemma endpoint list) and, for `CodingOrchestrator`, `self._editor_bases` (qwen-or-gemma endpoint list). All five model calls route through `acompletion_with_failover`.

Key regression guarantee: with no `LABMATE_FALLBACK_BASES`, `resolve_bases(self._gemma_base)` returns `[self._gemma_base]`, so `acompletion_with_failover` with `max_attempts_per_base=2` calls litellm with the exact same `model` / `api_base` / `api_key` / `**kwargs` as today, succeeding on the first attempt — behavior is identical plus a transient-blip retry.

- [ ] **Step 1: Write the failing wiring test**

```python
# append to tests/services/orchestrator/test_model_client.py
from unittest.mock import AsyncMock, patch
from services.orchestrator.coding_orchestrator import CodingOrchestrator


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_architect_routes_through_failover_wrapper(monkeypatch):
    monkeypatch.delenv("LABMATE_FALLBACK_BASES", raising=False)
    orch = CodingOrchestrator(
        graph=None, workspace_path=".", docker_container="",
        gemma_api_base="http://primary:8000/v1",
    )
    seen = {}

    async def fake_failover(*, model, bases, **kwargs):
        seen["model"] = model
        seen["bases"] = bases
        seen["extra_body"] = kwargs.get("extra_body")
        return _ok("planned")

    with patch(
        "services.orchestrator.coding_orchestrator.acompletion_with_failover",
        new=AsyncMock(side_effect=fake_failover),
    ):
        out = await orch.architect("plan this", thinking_budget=3000)

    assert out == "planned"
    assert seen["model"] == "openai/gemma-4-31b"
    assert seen["bases"] == ["http://primary:8000/v1"]
    assert seen["extra_body"] == {"thinking_budget_tokens": 3000}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/orchestrator/test_model_client.py -k architect_routes -v`
Expected: FAIL — either `AttributeError`/`ImportError` because `acompletion_with_failover` is not imported into `coding_orchestrator`, or the patch target does not exist yet.

- [ ] **Step 3: Implement the wire-in**

3a. Add the import at the top of `coding_orchestrator.py` (after the existing `from . import events` on line 14):

```python
from .model_client import acompletion_with_failover, resolve_bases
```

3b. In `AsyncOrchestrator.__init__`, after `self._gemma_base = gemma_api_base` (line 127), add:

```python
        # Ordered endpoint list for failover (primary + LABMATE_FALLBACK_BASES).
        self._bases = resolve_bases(gemma_api_base)
        self._editor_bases = resolve_bases(qwen_api_base)
```

3c. Replace the ReAct loop call (lines 415–423):

```python
                r = await acompletion_with_failover(
                    model="openai/gemma-4-31b",
                    bases=self._bases,
                    api_key="not-needed",
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    extra_body={"thinking_budget_tokens": 2048},
                )
```

3d. Replace the `aggregate` call (lines 686–692):

```python
        r = await acompletion_with_failover(
            model="openai/gemma-4-31b",
            bases=self._bases,
            api_key="not-needed",
            messages=[{"role": "user", "content": prompt}],
            extra_body={"thinking_budget_tokens": 2000},
        )
```

3e. In `CodingOrchestrator.__init__`, after `self._qwen_base = qwen_api_base` (line 728), add:

```python
        # Ordered endpoint lists for failover (primary + LABMATE_FALLBACK_BASES).
        self._bases = resolve_bases(gemma_api_base)
        self._editor_bases = resolve_bases(qwen_api_base)
```

3f. Replace the `architect` call (lines 797–804):

```python
        r = await acompletion_with_failover(
            model="openai/gemma-4-31b",
            bases=self._bases,
            api_key="not-needed",
            messages=self._build_messages(prompt),
            extra_body={"thinking_budget_tokens": thinking_budget},
        )
        return r.choices[0].message.content
```

3g. Replace the `editor` call (lines 812–819):

```python
        r = await acompletion_with_failover(
            model="openai/qwen2.5-coder-32b",
            bases=self._editor_bases,
            api_key="not-needed",
            messages=self._build_messages(prompt),
            extra_body={"thinking_budget_tokens": thinking_budget},
        )
        return r.choices[0].message.content
```

3h. Replace the `stream_final_answer` streaming call (lines 901–908). The wrapper forwards `stream=True` verbatim; on success it returns the streaming response object exactly as litellm would:

```python
            stream = await acompletion_with_failover(
                model="openai/gemma-4-31b",
                bases=self._bases,
                api_key="not-needed",
                messages=[{"role": "user", "content": prompt}],
                stream=True,
                extra_body={"thinking_budget_tokens": 0},
            )
```

Leave `_call_qwen_worker` (deprecated, unused, lines 657–671) untouched.

- [ ] **Step 4: Run the wiring test + the full existing orchestrator suite**

Run: `pytest tests/services/orchestrator/test_model_client.py -k architect_routes -v`
Expected: PASS.

Run: `pytest tests/services/orchestrator/test_coding_orchestrator.py -v`
Expected: PASS. The existing tests patch `services.orchestrator.coding_orchestrator.litellm.acompletion`. `stream_final_answer` and any path now calling `acompletion_with_failover` ultimately invoke `litellm.acompletion` (single base, first-attempt success), so the existing patches still intercept the real call. If any existing test asserts a *direct* `litellm.acompletion` call count and now sees it routed (still one call, single base), it remains green. If a test patched at a level that no longer matches, repoint that test's patch to `services.orchestrator.coding_orchestrator.acompletion_with_failover` — but verify first; the single-base path should keep `litellm.acompletion` patches working.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/coding_orchestrator.py tests/services/orchestrator/test_model_client.py
git commit -m "feat(orchestrator): route architect/editor/ReAct/aggregate/stream through failover client"
```

---

## Task 7: pytest-bdd feature + step defs

**Files:**
- Create: `tests/services/orchestrator/features/endpoint_failover.feature`
- Create: `tests/services/orchestrator/test_endpoint_failover_bdd.py`

**Interfaces:**
- Consumes: `acompletion_with_failover`, `AllEndpointsExhausted` (Task 4), respx, pytest-bdd.
- Assumes the foundation plan added `pytest-bdd` to the dependency set and a `tests/conftest.py`. This task depends only on `pytest-bdd` being importable; it does NOT use the `fake_model` fixture (it drives respx directly per the shared contract's "drive respx to fail/succeed per endpoint").

- [ ] **Step 1: Create the feature file**

Create `tests/services/orchestrator/features/endpoint_failover.feature` with the EXACT Gherkin from the "Behavior (BDD) — Gherkin" section above (copy it verbatim).

- [ ] **Step 2: Write the step defs (failing — module/feature not yet bound)**

```python
# tests/services/orchestrator/test_endpoint_failover_bdd.py
from __future__ import annotations

import httpx
import litellm
import pytest
import respx
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.model_client import (
    acompletion_with_failover,
    AllEndpointsExhausted,
)

pytestmark = pytest.mark.mocked

scenarios("features/endpoint_failover.feature")

A = "http://a:8000/v1"
B = "http://b:8000/v1"


def _completion(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"index": 0,
                           "message": {"role": "assistant", "content": content},
                           "finish_reason": "stop"}]},
    )


@pytest.fixture
def ctx():
    # Mutable bag shared across steps; holds respx router, config, and outcome.
    return {
        "bases": [],
        "attempts_cap": 2,
        "result": None,
        "error": None,
    }


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False) as r:
        yield r


# ── Background ────────────────────────────────────────────────────────────────
@given("backoff sleeping is disabled in the test harness")
def _no_sleep(ctx):
    async def _noop(_):
        return None
    ctx["sleep"] = _noop


@given("jitter is deterministic in the test harness")
def _det_jitter(ctx):
    ctx["rng"] = lambda: 0.0


# ── Given endpoints ──────────────────────────────────────────────────────────
@given("a primary base url that always returns connection errors")
def _primary_conn_err(ctx, router):
    router.post(f"{A}/chat/completions").mock(
        side_effect=httpx.ConnectError("refused")
    )
    ctx["bases"].append(A)


@given("a secondary base url that returns a valid completion")
def _secondary_ok(ctx, router):
    router.post(f"{B}/chat/completions").mock(return_value=_completion("from-b"))
    ctx["bases"].append(B)


@given("a primary base url that always returns 503 Service Unavailable")
def _primary_503(ctx, router):
    router.post(f"{A}/chat/completions").mock(
        return_value=httpx.Response(503, json={"error": {"message": "down"}})
    )
    ctx["bases"].append(A)


@given("a secondary base url that always returns connection errors")
def _secondary_conn_err(ctx, router):
    router.post(f"{B}/chat/completions").mock(
        side_effect=httpx.ConnectError("refused")
    )
    ctx["bases"].append(B)


@given("a primary base url that returns 400 Bad Request")
def _primary_400(ctx, router):
    router.post(f"{A}/chat/completions").mock(
        return_value=httpx.Response(400, json={"error": {"message": "bad"}})
    )
    ctx["bases"].append(A)


@given("a single base url that returns 503 once and then a valid completion")
def _single_blip(ctx, router):
    responses = [
        httpx.Response(503, json={"error": {"message": "down"}}),
        _completion("recovered"),
    ]
    router.post(f"{A}/chat/completions").mock(side_effect=responses)
    ctx["bases"].append(A)


@given(parsers.parse("the per-base attempt cap is {n:d}"))
def _set_cap(ctx, n):
    ctx["attempts_cap"] = n


# ── When ─────────────────────────────────────────────────────────────────────
@when(parsers.re(r"I request a completion with failover across (both endpoints|that endpoint)"))
async def _do_request(ctx):
    try:
        ctx["result"] = await acompletion_with_failover(
            model="openai/gemma-4-31b",
            bases=ctx["bases"],
            max_attempts_per_base=ctx["attempts_cap"],
            sleep=ctx["sleep"],
            rng=ctx["rng"],
            messages=[{"role": "user", "content": "hi"}],
            extra_body={"thinking_budget_tokens": 0},
        )
    except Exception as exc:  # noqa: BLE001 — captured for the Then steps
        ctx["error"] = exc


# ── Then ─────────────────────────────────────────────────────────────────────
@then(parsers.parse("the completion succeeds with the secondary endpoint's content"))
def _check_secondary_content(ctx):
    assert ctx["error"] is None
    assert ctx["result"].choices[0].message.content == "from-b"


@then("the secondary endpoint was called after the primary")
def _check_secondary_called(ctx, router):
    route = router.routes[-1]  # secondary registered last
    assert route.call_count == 1


@then("an AllEndpointsExhausted error is raised")
def _check_exhausted(ctx):
    assert isinstance(ctx["error"], AllEndpointsExhausted)


@then(parsers.parse("the primary endpoint was attempted exactly {n:d} times"))
def _check_primary_attempts(ctx, router, n):
    assert router.routes[0].call_count == n


@then(parsers.parse("the secondary endpoint was attempted exactly {n:d} times"))
def _check_secondary_attempts(ctx, router, n):
    assert router.routes[1].call_count == n


@then("a BadRequestError is raised")
def _check_badrequest(ctx):
    assert isinstance(ctx["error"], litellm.BadRequestError)


@then("the secondary endpoint was never called")
def _check_secondary_never(ctx, router):
    assert router.routes[1].call_count == 0


@then("the completion succeeds with that endpoint's content")
def _check_blip_content(ctx):
    assert ctx["error"] is None
    assert ctx["result"].choices[0].message.content == "recovered"


@then(parsers.parse("the endpoint was attempted exactly {n:d} times"))
def _check_single_attempts(ctx, router, n):
    assert router.routes[0].call_count == n
```

Note on async steps: pytest-bdd runs sync step functions by default. The `@when` step here is `async`. The foundation plan's conftest is assumed to enable async step execution (e.g. `pytest-bdd`'s asyncio support or an `anyio`/`pytest-asyncio` integration). If async steps are NOT supported by the foundation setup, change `_do_request` to a sync wrapper:

```python
@when(parsers.re(r"I request a completion with failover across (both endpoints|that endpoint)"))
def _do_request(ctx):
    import asyncio
    try:
        ctx["result"] = asyncio.get_event_loop().run_until_complete(
            acompletion_with_failover(
                model="openai/gemma-4-31b",
                bases=ctx["bases"],
                max_attempts_per_base=ctx["attempts_cap"],
                sleep=ctx["sleep"],
                rng=ctx["rng"],
                messages=[{"role": "user", "content": "hi"}],
                extra_body={"thinking_budget_tokens": 0},
            )
        )
    except Exception as exc:  # noqa: BLE001
        ctx["error"] = exc
```

Prefer the async form if the foundation conftest supports it; fall back to the sync `run_until_complete` wrapper otherwise.

- [ ] **Step 3: Run the BDD suite to verify it fails first, then passes**

Run: `pytest tests/services/orchestrator/test_endpoint_failover_bdd.py -v`
Expected (before wrapper exists): collection or import error. After Tasks 1–4 are done, expected: PASS — all 4 scenarios green.

If pytest-bdd is not installed (the foundation plan has not landed), this command errors with `ModuleNotFoundError: No module named 'pytest_bdd'`. That is the dependency the foundation plan owns; note it and proceed only once it is present.

- [ ] **Step 4: Run the whole orchestrator test directory**

Run: `pytest tests/services/orchestrator/ -q`
Expected: PASS (no regressions; failover unit tests + BDD scenarios + existing suite all green).

- [ ] **Step 5: Commit**

```bash
git add tests/services/orchestrator/features/endpoint_failover.feature tests/services/orchestrator/test_endpoint_failover_bdd.py
git commit -m "test(orchestrator): pytest-bdd endpoint failover scenarios"
```

---

## Self-Review

**1. Spec coverage** (feature requirements → tasks):

| Requirement | Task |
|---|---|
| Ordered base-URL list (primary GEMMA_BASE + LABMATE_FALLBACK_BASES) | Task 2 (`resolve_bases`) |
| Auto failover on retryable transport errors (conn-refused, 5xx, timeout) | Task 1 (`is_retryable`) + Task 4 (loop) |
| Bounded attempts + jittered backoff | Task 3 (`backoff_delay`) + Task 4 (cap) |
| Terminal error when all endpoints exhausted | Task 4 (`AllEndpointsExhausted`) |
| Do NOT fail over on 4xx content errors | Task 1 (`_TERMINAL` first) + Task 4 (re-raise) + BDD scenario 3 |
| Route architect/editor/ReAct through wrapper | Task 6 |
| Single endpoint, no failures == today + bounded retry | Task 5 (respx regression) + Task 6 regression note |
| respx-testable endpoint selection + retry/failover | Task 5 + Task 7 |
| Test-friendly backoff (inject/seed/zero sleep) | Tasks 3, 4 (`sleep`/`rng` params) |
| BDD: primary down → secondary; all down → terminal; 4xx → no failover; single-endpoint blip → recover | Task 7 (4 scenarios) |
| No tiktoken / stdout clean / asyncio-correct / thinking_budget preserved | Global Constraints; wrapper forwards `extra_body` verbatim, uses `logging`, `await asyncio.sleep` |

No gaps.

**2. Placeholder scan:** No "TBD"/"implement later"/"add error handling" placeholders — every code step contains the actual code. The only conditional is Task 5 Step 3 (widen `_RETRYABLE` only if the observed litellm class differs), which is a real, bounded TDD branch with explicit instructions, not a placeholder.

**3. Type consistency:**
- `is_retryable(exc) -> bool` — defined Task 1, used Task 4. ✔
- `resolve_bases(primary, fallbacks_env=None) -> list[str]` — defined Task 2, used Task 6. ✔
- `backoff_delay(attempt, base_s, max_s, rng) -> float` — defined Task 3, used Task 4. ✔
- `acompletion_with_failover(*, model, bases, api_key, max_attempts_per_base, base_backoff_s, max_backoff_s, sleep, rng, _acompletion, **kwargs)` — defined Task 4, used Tasks 5, 6, 7. All call sites pass `model=`, `bases=`, and forward `messages`/`extra_body`. ✔
- `AllEndpointsExhausted(attempts)` with `.attempts` — defined Task 4, asserted Tasks 4, 7. ✔
- `self._bases` / `self._editor_bases` — set Task 6 `__init__`, used Task 6 call sites and wiring test. ✔

All names consistent across tasks. Plan complete.
