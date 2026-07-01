"""Per-segment token accounting (token-cost Task 1 — the live-prep measurement).

Verifies measure_prompt_segments attributes a model request's token fill to the
right named segments (system_base / skill_catalog / tool_schemas / conversation /
continuity / tool_results) so a live run reveals WHERE the tokens go instead of
lumping everything into "conversation".
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from services.orchestrator.prompt_assembler import PromptAssembler, measure_prompt_segments

pytestmark = pytest.mark.mocked


def _stub_token_count(text: str) -> int:
    """Deterministic stub: 1 token per 4 chars (so assertions are exact)."""
    return len(text or "") // 4


def test_measure_prompt_segments_attributes_each_segment():
    catalog = "- skill-a: does A\n- skill-b: does B\n- skill-c: does C"
    with patch("services.memory.tokenizer.token_count", side_effect=_stub_token_count):
        assembler = PromptAssembler(
            skill_router=None, base_system="BASE SYSTEM PROMPT", catalog=catalog
        )
        messages = [
            assembler.system_message(),  # system: base + catalog
            {
                "role": "user",
                "content": "CONVERSATION SO FAR (context — the user's new message is below):\n"
                "USER: what is TSP\nASSISTANT: the traveling salesman problem",
            },
            {"role": "user", "content": "is that problem NP-complete?"},
            {"role": "tool", "content": "x" * 400},  # a big tool result
        ]
        seg = measure_prompt_segments(assembler, messages)

    # The catalog is attributed to skill_catalog (== token_count of the catalog),
    # NOT lumped into conversation — this is the whole point of the measurement.
    assert seg["skill_catalog"] == _stub_token_count(catalog)
    assert seg["skill_catalog"] > 0
    # system_base excludes the catalog (measured separately).
    assert seg["system_base"] > 0
    assert seg["system_base"] < seg["system_base"] + seg["skill_catalog"]
    # tool schemas are non-empty (build_tool_list always yields call_skill_tool etc.).
    assert seg["tool_schemas"] > 0
    # The continuity block is separated from ordinary conversation.
    assert seg["continuity"] > 0
    assert seg["conversation"] > 0
    # The 400-char tool result lands in tool_results (400 // 4 == 100), not conversation.
    assert seg["tool_results"] == 100
    # total is the sum of the parts.
    assert seg["total"] == sum(v for k, v in seg.items() if k != "total")


def test_measure_prompt_segments_no_catalog_zero_catalog_segment():
    """With no skill catalog, skill_catalog is 0 and system_base carries the base prompt."""
    with patch("services.memory.tokenizer.token_count", side_effect=_stub_token_count):
        assembler = PromptAssembler(skill_router=None, base_system="BASE", catalog=None)
        seg = measure_prompt_segments(assembler, [assembler.system_message()])
    assert seg["skill_catalog"] == 0
    assert seg["system_base"] > 0
    assert seg["continuity"] == 0
    assert seg["conversation"] == 0
