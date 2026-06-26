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

import asyncio
import json

import httpx
import litellm
import pytest

# The inference seam every orchestrator model call routes through.
# Source of truth: services/orchestrator/graph.py GEMMA_BASE default.
INFERENCE_COMPLETIONS_URL = "http://localhost:8000/v1/chat/completions"


def run_async(coro):
    """Run an async coroutine and clean up pending tasks.

    This helper cancels and awaits all pending tasks before closing the event
    loop, preventing "coroutine '...' was never awaited" RuntimeWarnings from
    background tasks (OutboxWorker.run, litellm logging workers, etc.) that
    are spawned incidentally but not essential to the test assertion.

    Use in place of bare asyncio.run() in BDD step defs.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        # Cancel and await all pending tasks to allow cleanup
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()


@pytest.fixture
async def fake_model(respx_mock):
    """Program the inference seam to return a deterministic completion.

    Returns a callable used inside @given steps (or directly in a test)
    before the agent issues a model call:

        # tool-call completion
        fake_model("edit_file", {"path": "src/app.py", "content": "..."})

        # plain-content completion (no tool call)
        fake_model(None, content="2 + 2 = 4")

    The last call wins: re-calling re-programs the same route.

    Implementation: Creates an httpx.AsyncClient and sets it as
    litellm.aclient_session so litellm's OpenAI client uses it.
    respx intercepts all HTTP calls through this client.
    """
    # Create an httpx client that respx can intercept
    async with httpx.AsyncClient() as client:
        # Store the original aclient_session
        original_session = getattr(litellm, "aclient_session", None)

        # Set our client as litellm's session
        litellm.aclient_session = client

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
            # Program respx to intercept and mock the inference HTTP seam
            respx_mock.post(INFERENCE_COMPLETIONS_URL).mock(
                return_value=httpx.Response(200, json=body)
            )

        yield _set

        # Restore original aclient_session
        litellm.aclient_session = original_session
