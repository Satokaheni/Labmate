from __future__ import annotations
import json
import pytest
from services.orchestrator.prompt_assembler import PromptAssembler, BASE_SYSTEM_PROMPT


@pytest.mark.mocked
def test_canonical_prefix_is_byte_identical_across_two_instances():
    a = PromptAssembler(skill_router=None, codegraph_enabled=False)
    b = PromptAssembler(skill_router=None, codegraph_enabled=False)
    assert a.canonical_prefix() == b.canonical_prefix()
    assert a.prefix_fingerprint() == b.prefix_fingerprint()


@pytest.mark.mocked
def test_system_message_and_tools_return_same_object_each_call():
    a = PromptAssembler(skill_router=None, codegraph_enabled=False)
    assert a.system_message() is a.system_message()      # frozen object reuse
    assert a.tools() is a.tools()


@pytest.mark.mocked
def test_canonical_prefix_uses_sorted_keys_and_is_valid_json():
    a = PromptAssembler(skill_router=None, codegraph_enabled=False)
    prefix = a.canonical_prefix()
    parsed = json.loads(prefix)                            # must be valid JSON
    assert set(parsed.keys()) == {"system", "tools"}
    # sort_keys means re-dumping with the same options is a fixed point
    redump = json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert redump == prefix
