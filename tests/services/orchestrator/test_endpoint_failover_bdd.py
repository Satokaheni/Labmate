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
def router(respx_mock):
    """Use the built-in pytest-respx fixture for proper isolation."""
    # Configure respx to not require all routes to be called
    respx_mock.assert_all_called = False
    return respx_mock


@pytest.fixture
def ctx(router):
    # Mutable bag shared across steps; holds respx router, config, and outcome.
    return {
        "bases": [],
        "attempts_cap": 2,
        "result": None,
        "error": None,
    }


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
def _do_request(ctx, router):
    import asyncio

    async def _main():
        # Create an httpx client that respx can intercept
        async with httpx.AsyncClient() as client:
            # Store the original aclient_session and num_retries
            original_session = getattr(litellm, "aclient_session", None)
            original_retries = getattr(litellm, "num_retries", None)

            # Set our client as litellm's session so respx can intercept calls
            # Disable litellm's internal retries so our failover handles all retries
            litellm.aclient_session = client
            litellm.num_retries = 0

            try:
                result = await acompletion_with_failover(
                    model="openai/gemma-4-31b",
                    bases=ctx["bases"],
                    max_attempts_per_base=ctx["attempts_cap"],
                    sleep=ctx["sleep"],
                    rng=ctx["rng"],
                    messages=[{"role": "user", "content": "hi"}],
                    extra_body={"thinking_budget_tokens": 0},
                    num_retries=0,
                )
                return result
            finally:
                # Restore original settings
                litellm.aclient_session = original_session
                litellm.num_retries = original_retries

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
