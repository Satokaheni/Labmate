from __future__ import annotations

import pytest
import litellm

from services.orchestrator.model_client import is_retryable, resolve_bases


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


import respx
import httpx


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_single_endpoint_no_failures_matches_direct_call(respx_mock):
    # llama.cpp OpenAI-compatible chat completions endpoint.
    # Set up httpx client in litellm to be intercepted by respx
    async with httpx.AsyncClient() as client:
        original_session = getattr(litellm, "aclient_session", None)
        litellm.aclient_session = client
        try:
            route = respx_mock.post("http://a:8000/v1/chat/completions").mock(
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
        finally:
            litellm.aclient_session = original_session


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_respx_primary_503_then_secondary_200(respx_mock):
    # Set up httpx client in litellm to be intercepted by respx
    async with httpx.AsyncClient() as client:
        original_session = getattr(litellm, "aclient_session", None)
        litellm.aclient_session = client
        try:
            respx_mock.post("http://a:8000/v1/chat/completions").mock(
                return_value=httpx.Response(503, json={"error": {"message": "down"}})
            )
            secondary = respx_mock.post("http://b:8000/v1/chat/completions").mock(
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
        finally:
            litellm.aclient_session = original_session


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
