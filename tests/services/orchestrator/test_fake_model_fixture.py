"""Proves the shared fake_model fixture mocks the inference HTTP seam.

The orchestrator talks to llama.cpp via litellm with
api_base="http://localhost:8000/v1" and model="openai/gemma-4-31b",
which issues POST http://localhost:8000/v1/chat/completions. These tests
exercise that exact seam through litellm so the fixture is validated against
the real call path, not a hand-rolled httpx request.
"""
from __future__ import annotations

import json
import pytest
import litellm


@pytest.mark.mocked
async def test_fake_model_programs_a_tool_call(fake_model):
    fake_model("edit_file", {"path": "src/app.py", "content": "print(1)"})

    resp = await litellm.acompletion(
        model="openai/gemma-4-31b",
        api_base="http://localhost:8000/v1",
        api_key="not-needed",
        messages=[{"role": "user", "content": "edit the file"}],
    )

    tool_calls = resp.choices[0].message.tool_calls
    assert tool_calls is not None
    assert tool_calls[0].function.name == "edit_file"
    assert json.loads(tool_calls[0].function.arguments) == {
        "path": "src/app.py",
        "content": "print(1)",
    }


@pytest.mark.mocked
async def test_fake_model_programs_plain_content(fake_model):
    fake_model(None, content="2 + 2 = 4")

    resp = await litellm.acompletion(
        model="openai/gemma-4-31b",
        api_base="http://localhost:8000/v1",
        api_key="not-needed",
        messages=[{"role": "user", "content": "what is 2+2?"}],
    )

    msg = resp.choices[0].message
    assert msg.tool_calls in (None, [])
    assert msg.content == "2 + 2 = 4"
