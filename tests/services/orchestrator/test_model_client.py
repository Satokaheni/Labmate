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
