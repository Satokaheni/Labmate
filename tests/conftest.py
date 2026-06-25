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
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

# The inference seam every orchestrator model call routes through.
# Source of truth: services/orchestrator/graph.py GEMMA_BASE default.
INFERENCE_COMPLETIONS_URL = "http://localhost:8000/v1/chat/completions"


@pytest.fixture
def fake_model(respx_mock, monkeypatch):
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
        # Set up respx mock for httpx-based clients
        respx_mock.post(INFERENCE_COMPLETIONS_URL).mock(
            return_value=httpx.Response(200, json=body)
        )

        # Also create a mock response object for litellm
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message = MagicMock()
        mock_response.choices[0].message.content = body["choices"][0]["message"].get(
            "content"
        )

        # Parse tool_calls from the body and create proper mock objects
        tool_calls_data = body["choices"][0]["message"].get("tool_calls")
        if tool_calls_data:
            tool_calls = []
            for tc in tool_calls_data:
                mock_tool_call = MagicMock()
                mock_tool_call.function = MagicMock()
                mock_tool_call.function.name = tc["function"]["name"]
                mock_tool_call.function.arguments = tc["function"]["arguments"]
                tool_calls.append(mock_tool_call)
            mock_response.choices[0].message.tool_calls = tool_calls
        else:
            mock_response.choices[0].message.tool_calls = None

        # Patch litellm.acompletion to return the mock response
        async def mock_acompletion(*args, **kwargs):
            return mock_response

        monkeypatch.setattr("litellm.acompletion", mock_acompletion)

    return _set
