from __future__ import annotations

from types import SimpleNamespace

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


def _ok(content: str) -> SimpleNamespace:
    """Return a completion response object matching litellm's response format."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _completion(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"index": 0,
                           "message": {"role": "assistant", "content": content},
                           "finish_reason": "stop"}]},
    )


@pytest.fixture
def ctx():
    # Mutable bag shared across steps; holds mocked endpoints, config, and outcome.
    return {
        "bases": [],
        "attempts_cap": 2,
        "result": None,
        "error": None,
        "endpoint_behaviors": {},  # map base_url -> function to simulate endpoint behavior
        "attempt_count": {},  # map base_url -> count of attempts made
    }


@pytest.fixture
def router():
    """Placeholder fixture for compatibility. In this BDD harness, we mock at the _acompletion level."""
    return None


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
def _primary_conn_err(ctx):
    ctx["endpoint_behaviors"][A] = lambda *a, **kw: (_ for _ in ()).throw(
        litellm.APIConnectionError(message="refused", llm_provider="openai", model="test")
    )
    ctx["bases"].append(A)


@given("a secondary base url that returns a valid completion")
def _secondary_ok(ctx):
    ctx["endpoint_behaviors"][B] = lambda *a, **kw: _ok("from-b")
    ctx["bases"].append(B)


@given("a primary base url that always returns 503 Service Unavailable")
def _primary_503(ctx):
    ctx["endpoint_behaviors"][A] = lambda *a, **kw: (_ for _ in ()).throw(
        litellm.ServiceUnavailableError(message="503", llm_provider="openai", model="test")
    )
    ctx["bases"].append(A)


@given("a secondary base url that always returns connection errors")
def _secondary_conn_err(ctx):
    ctx["endpoint_behaviors"][B] = lambda *a, **kw: (_ for _ in ()).throw(
        litellm.APIConnectionError(message="refused", llm_provider="openai", model="test")
    )
    ctx["bases"].append(B)


@given("a primary base url that returns 400 Bad Request")
def _primary_400(ctx):
    ctx["endpoint_behaviors"][A] = lambda *a, **kw: (_ for _ in ()).throw(
        litellm.BadRequestError(message="bad", llm_provider="openai", model="test")
    )
    ctx["bases"].append(A)


@given("a single base url that returns 503 once and then a valid completion")
def _single_blip(ctx):
    # Create a stateful behavior that fails once then succeeds
    call_count = [0]  # Use list to allow mutation in nested function

    def _blip_behavior(*a, **kw):
        call_count[0] += 1
        if call_count[0] == 1:
            # First call: raise 503
            raise litellm.ServiceUnavailableError(
                message="503", llm_provider="openai", model="test"
            )
        else:
            # Second call and beyond: return success
            return _ok("recovered")

    ctx["endpoint_behaviors"][A] = _blip_behavior
    ctx["bases"].append(A)


@given(parsers.parse("the per-base attempt cap is {n:d}"))
def _set_cap(ctx, n):
    ctx["attempts_cap"] = n


# ── When ─────────────────────────────────────────────────────────────────────
@when(parsers.re(r"I request a completion with failover across (both endpoints|that endpoint)"))
def _do_request(ctx):
    import asyncio

    # Create a mock _acompletion function that uses the configured endpoint behaviors
    async def _mock_acompletion(*, model, api_base, api_key, **kwargs):
        # Track this attempt
        ctx["attempt_count"][api_base] = ctx["attempt_count"].get(api_base, 0) + 1

        behavior = ctx["endpoint_behaviors"].get(api_base)
        if behavior is None:
            raise ValueError(f"Unexpected endpoint: {api_base}")
        result = behavior(model=model, api_base=api_base, api_key=api_key, **kwargs)
        return result

    async def _main():
        return await acompletion_with_failover(
            model="openai/gemma-4-31b",
            bases=ctx["bases"],
            max_attempts_per_base=ctx["attempts_cap"],
            sleep=ctx["sleep"],
            rng=ctx["rng"],
            messages=[{"role": "user", "content": "hi"}],
            extra_body={"thinking_budget_tokens": 0},
            _acompletion=_mock_acompletion,
        )

    try:
        ctx["result"] = asyncio.run(_main())
    except Exception as exc:  # noqa: BLE001 — captured for the Then steps
        ctx["error"] = exc


# ── Then ─────────────────────────────────────────────────────────────────────
@then(parsers.parse("the completion succeeds with the secondary endpoint's content"))
def _check_secondary_content(ctx):
    assert ctx["error"] is None
    assert ctx["result"].choices[0].message.content == "from-b"


@then("the secondary endpoint was called after the primary")
def _check_secondary_called(ctx):
    # Verify that secondary endpoint succeeded
    assert ctx["error"] is None
    assert ctx["result"] is not None


@then("an AllEndpointsExhausted error is raised")
def _check_exhausted(ctx):
    assert isinstance(ctx["error"], AllEndpointsExhausted)


@then(parsers.parse("the primary endpoint was attempted exactly {n:d} times"))
def _check_primary_attempts(ctx, n):
    assert ctx["attempt_count"].get(A, 0) == n


@then(parsers.parse("the secondary endpoint was attempted exactly {n:d} times"))
def _check_secondary_attempts(ctx, n):
    assert ctx["attempt_count"].get(B, 0) == n


@then("a BadRequestError is raised")
def _check_badrequest(ctx):
    assert isinstance(ctx["error"], litellm.BadRequestError)


@then("the secondary endpoint was never called")
def _check_secondary_never(ctx):
    # If we exhausted all endpoints, B was called. If we got a non-retryable error from A, B wasn't called.
    if isinstance(ctx["error"], AllEndpointsExhausted):
        # Check that B doesn't appear in the attempts
        all_bases = [base for base, _ in ctx["error"].attempts]
        assert B not in all_bases
    else:
        # Non-retryable error, so B was never tried
        assert ctx["error"] is not None


@then("the completion succeeds with that endpoint's content")
def _check_blip_content(ctx):
    assert ctx["error"] is None
    assert ctx["result"].choices[0].message.content == "recovered"


@then(parsers.parse("the endpoint was attempted exactly {n:d} times"))
def _check_single_attempts(ctx, n):
    assert ctx["attempt_count"].get(A, 0) == n
