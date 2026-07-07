# tests/services/orchestrator/test_thinking_budget_self_heal.py
"""Unit tests for the G5 thinking_budget_tokens self-heal in model_client.

CLAUDE.md rule #6 sends extra_body={"thinking_budget_tokens": N} on every model
call. A served build/model that does NOT recognize the param answers 400, and a
4xx is terminal (no failover) — so without self-heal EVERY call hard-fails. G5
detects that specific rejection, strips the param, retries once, and caches the
base so later calls skip the param up front.

Driven through the acompletion_with_failover `_acompletion` injection seam with a
fake async client that inspects the kwargs it receives and raises litellm-shaped
errors — no HTTP, no GPU.
"""

from __future__ import annotations

import litellm
import pytest

from services import model_client
from services.model_client import acompletion_with_failover


@pytest.fixture(autouse=True)
def _clear_cache():
    """Isolate the process-wide learned-base cache between tests."""
    model_client._NO_THINKING_BUDGET_BASES.clear()
    yield
    model_client._NO_THINKING_BUDGET_BASES.clear()


def _bad_request(msg: str) -> litellm.BadRequestError:
    return litellm.BadRequestError(message=msg, model="m", llm_provider="openai")


def _make_fake(record: list[dict], script):
    """Build a fake _acompletion. `script` is a callable(call_index, kwargs) that
    returns a value to return, or an Exception instance to raise. Each call's
    kwargs are appended to `record` first."""
    state = {"i": 0}

    async def _fake(**kwargs):
        record.append(kwargs)
        i = state["i"]
        state["i"] += 1
        out = script(i, kwargs)
        if isinstance(out, Exception):
            raise out
        return out

    return _fake


async def _call(fake, **overrides):
    kwargs = dict(
        model="openai/gemma-4-31b",
        bases=["http://primary/v1"],
        messages=[{"role": "user", "content": "hi"}],
        extra_body={"thinking_budget_tokens": 2048},
        _acompletion=fake,
    )
    kwargs.update(overrides)
    return await acompletion_with_failover(**kwargs)


@pytest.mark.asyncio
async def test_self_heal_strips_param_and_retries_once():
    """First call (param present) 400s naming the field; the retry (param
    stripped) succeeds. The base is cached and the sentinel is returned."""
    record: list[dict] = []

    def script(i, kwargs):
        if i == 0:
            return _bad_request("Unknown field: thinking_budget_tokens")
        return "OK"

    result = await _call(_make_fake(record, script))

    assert result == "OK"
    assert len(record) == 2
    # First attempt carried the param; the healed retry dropped it.
    assert "thinking_budget_tokens" in record[0]["extra_body"]
    assert "thinking_budget_tokens" not in record[1]["extra_body"]
    # Base learned/cached.
    assert "http://primary/v1" in model_client._NO_THINKING_BUDGET_BASES


@pytest.mark.asyncio
async def test_cached_base_strips_param_up_front_on_next_call():
    """Once a base is cached, a subsequent failover call strips the param BEFORE
    the first attempt (no rejection round-trip)."""
    model_client._NO_THINKING_BUDGET_BASES.add("http://primary/v1")
    record: list[dict] = []

    result = await _call(_make_fake(record, lambda i, kw: "OK"))

    assert result == "OK"
    assert len(record) == 1  # no rejection, single attempt
    assert "thinking_budget_tokens" not in record[0]["extra_body"]


@pytest.mark.asyncio
async def test_generic_400_is_not_self_healed():
    """A 400 that does NOT name the param (a real bad request) stays terminal:
    re-raised, param NOT stripped, base NOT cached."""
    record: list[dict] = []

    def script(i, kwargs):
        return _bad_request("you must provide a messages field")

    with pytest.raises(litellm.BadRequestError):
        await _call(_make_fake(record, script))

    assert len(record) == 1  # no heal retry
    assert model_client._NO_THINKING_BUDGET_BASES == set()


@pytest.mark.asyncio
async def test_value_rejection_is_not_self_healed():
    """A 400 that NAMES the param but complains about the VALUE (a build that
    HONORS the field but dislikes the number) must NOT strip — stripping would
    drop a param the server honors → INT_MAX-default hang (rule #6)."""
    record: list[dict] = []

    def script(i, kwargs):
        return _bad_request("unsupported value for thinking_budget_tokens: must be >= 0")

    with pytest.raises(litellm.BadRequestError):
        await _call(_make_fake(record, script))

    assert len(record) == 1  # no heal retry
    assert model_client._NO_THINKING_BUDGET_BASES == set()


@pytest.mark.asyncio
async def test_healed_retry_failure_is_reraised_not_looped():
    """If the healed (param-stripped) retry itself fails terminally, that error
    is re-raised — not swallowed, not retried forever. Exactly two calls."""
    record: list[dict] = []

    def script(i, kwargs):
        if i == 0:
            return _bad_request("unknown field: thinking_budget_tokens")
        return _bad_request("still broken for another reason")

    with pytest.raises(litellm.BadRequestError, match="still broken"):
        await _call(_make_fake(record, script))

    assert len(record) == 2  # original + one heal retry, then re-raised
    # The base was still cached from the rejection (the strip did happen).
    assert "http://primary/v1" in model_client._NO_THINKING_BUDGET_BASES


@pytest.mark.asyncio
async def test_no_op_when_param_accepted():
    """When the server accepts the param, nothing is stripped or cached."""
    record: list[dict] = []

    result = await _call(_make_fake(record, lambda i, kw: "OK"))

    assert result == "OK"
    assert len(record) == 1
    assert record[0]["extra_body"] == {"thinking_budget_tokens": 2048}
    assert model_client._NO_THINKING_BUDGET_BASES == set()


@pytest.mark.asyncio
async def test_5xx_is_not_treated_as_param_rejection():
    """A retryable 5xx is normal failover territory, not a param rejection — the
    base is never cached even though it fails."""
    record: list[dict] = []

    def script(i, kwargs):
        return litellm.InternalServerError(message="server down", model="m", llm_provider="openai")

    with pytest.raises(model_client.AllEndpointsExhausted):
        await _call(_make_fake(record, script), max_attempts_per_base=2, sleep=_no_sleep)

    assert model_client._NO_THINKING_BUDGET_BASES == set()
    # Every attempt still carried the param (never stripped).
    assert all("thinking_budget_tokens" in r["extra_body"] for r in record)


@pytest.mark.asyncio
async def test_self_heal_disabled_by_env(monkeypatch):
    """With the kill-switch off, a param-rejection 400 is terminal (re-raised),
    matching the pre-G5 behavior."""
    monkeypatch.setattr(model_client, "_THINKING_BUDGET_SELF_HEAL", False)
    record: list[dict] = []

    def script(i, kwargs):
        return _bad_request("unknown field: thinking_budget_tokens")

    with pytest.raises(litellm.BadRequestError):
        await _call(_make_fake(record, script))

    assert len(record) == 1
    assert model_client._NO_THINKING_BUDGET_BASES == set()


async def _no_sleep(_seconds):
    return None
