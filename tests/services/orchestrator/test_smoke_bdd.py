"""Step definitions for the BDD harness smoke feature.

Canonical worked example for the Shared BDD Contract:
  - feature lives at features/smoke.feature (tagged @mocked)
  - step-def file is test_<slug>_bdd.py beside the unit tests
  - scenarios(...) path is relative to THIS file's directory
  - the @mocked Gherkin tag maps to the pytest 'mocked' marker

See docs/superpowers/plans/2026-06-25-bdd-harness-foundation.md
"""
from __future__ import annotations

import json

import litellm
import pytest
from pytest_bdd import scenarios, given, when, then, parsers

pytestmark = [pytest.mark.bdd, pytest.mark.mocked]

# Bind every Scenario in smoke.feature to the step defs below.
scenarios("features/smoke.feature")


@pytest.fixture
def reply_box() -> dict:
    """Per-scenario mutable holder for the model response."""
    return {}


@given(parsers.parse('the model is programmed to answer "{answer}"'))
def _program_plain_answer(fake_model, answer: str) -> None:
    fake_model(None, content=answer)


@given(
    parsers.parse('the model is programmed to call tool "{tool}" with path "{path}"')
)
def _program_tool_call(fake_model, tool: str, path: str) -> None:
    fake_model(tool, {"path": path})


@when("the orchestrator asks the model a question")
def _ask_model(reply_box: dict) -> None:
    import asyncio

    async def _call():
        return await litellm.acompletion(
            model="openai/gemma-4-31b",
            api_base="http://localhost:8000/v1",
            api_key="not-needed",
            messages=[{"role": "user", "content": "anything"}],
        )

    reply_box["resp"] = asyncio.run(_call())


@then(parsers.parse('the model reply is "{expected}"'))
def _assert_reply(reply_box: dict, expected: str) -> None:
    msg = reply_box["resp"].choices[0].message
    assert msg.content == expected


@then(parsers.parse('the model requests tool "{tool}"'))
def _assert_tool(reply_box: dict, tool: str) -> None:
    tool_calls = reply_box["resp"].choices[0].message.tool_calls
    assert tool_calls is not None
    assert tool_calls[0].function.name == tool
    # arguments are valid JSON
    json.loads(tool_calls[0].function.arguments)
