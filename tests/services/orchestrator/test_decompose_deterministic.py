from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def make_runner() -> MagicMock:
    runner = MagicMock()
    runner.catalog = {"a": "x", "b": "y"}
    runner.catalog_prompt.return_value = "CATALOG"
    runner.tool_schema.return_value = {"type": "function", "function": {"name": "load_skill"}}
    return runner


def make_redis() -> MagicMock:
    redis = MagicMock()
    redis.xadd = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    return redis


def content_response(text: str) -> MagicMock:
    """A litellm response carrying plain text content."""
    message = MagicMock()
    message.content = text
    message.tool_calls = None
    return MagicMock(choices=[MagicMock(message=message)])


def make_router():
    from services.orchestrator.skill_router import SkillRouter

    return SkillRouter(make_runner(), make_redis(), "http://localhost:8000/v1")


@pytest.mark.asyncio
async def test_decompose_uses_deterministic_decoding():
    """decompose() must call litellm with temperature=0 and seed=0 (FIX 6)."""
    router = make_router()
    mock_acompletion = AsyncMock(return_value=content_response('["one task"]'))
    with patch("services.orchestrator.skill_router.litellm.acompletion", mock_acompletion):
        await router.decompose("do one thing")
    assert mock_acompletion.call_count == 1
    kwargs = mock_acompletion.call_args.kwargs
    assert kwargs["temperature"] == 0
    assert kwargs["seed"] == 0
    # thinking_budget stays at 512
    assert kwargs["extra_body"] == {"thinking_budget_tokens": 512}


@pytest.mark.asyncio
async def test_decompose_two_element_response_yields_two():
    router = make_router()
    resp = content_response(
        '["Write a Python function that reverses a string", '
        '"Write a pytest unit test for the string-reversal function"]'
    )
    with patch(
        "services.orchestrator.skill_router.litellm.acompletion",
        AsyncMock(return_value=resp),
    ):
        out = await router.decompose(
            "Write a function that reverses a string, and write a pytest unit test for it"
        )
    assert out == [
        "Write a Python function that reverses a string",
        "Write a pytest unit test for the string-reversal function",
    ]


@pytest.mark.asyncio
async def test_decompose_single_element_response_yields_one():
    router = make_router()
    with patch(
        "services.orchestrator.skill_router.litellm.acompletion",
        AsyncMock(return_value=content_response('["Summarize this PDF"]')),
    ):
        out = await router.decompose("Summarize this PDF")
    assert out == ["Summarize this PDF"]


@pytest.mark.asyncio
async def test_decompose_non_json_fails_open_to_task():
    router = make_router()
    task = "some compound task"
    with patch(
        "services.orchestrator.skill_router.litellm.acompletion",
        AsyncMock(return_value=content_response("not json at all")),
    ):
        out = await router.decompose(task)
    assert out == [task]


@pytest.mark.asyncio
async def test_decompose_llm_error_fails_open_to_task():
    router = make_router()
    task = "another task"
    with patch(
        "services.orchestrator.skill_router.litellm.acompletion",
        AsyncMock(side_effect=RuntimeError("boom")),
    ):
        out = await router.decompose(task)
    assert out == [task]


@pytest.mark.asyncio
async def test_decompose_caps_at_four():
    router = make_router()
    resp = content_response('["a", "b", "c", "d", "e", "f"]')
    with patch(
        "services.orchestrator.skill_router.litellm.acompletion",
        AsyncMock(return_value=resp),
    ):
        out = await router.decompose("big task")
    assert out == ["a", "b", "c", "d"]
