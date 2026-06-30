from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from services.orchestrator.prompt_assembler import BASE_SYSTEM_PROMPT, PromptAssembler


@pytest.mark.mocked
def test_canonical_prefix_is_byte_identical_across_two_instances():
    a = PromptAssembler(skill_router=None, codegraph_enabled=False)
    b = PromptAssembler(skill_router=None, codegraph_enabled=False)
    assert a.canonical_prefix() == b.canonical_prefix()
    assert a.prefix_fingerprint() == b.prefix_fingerprint()


@pytest.mark.mocked
def test_system_message_and_tools_return_same_object_each_call():
    a = PromptAssembler(skill_router=None, codegraph_enabled=False)
    assert a.system_message() is a.system_message()  # frozen object reuse
    assert a.tools() is a.tools()


@pytest.mark.mocked
def test_canonical_prefix_uses_sorted_keys_and_is_valid_json():
    a = PromptAssembler(skill_router=None, codegraph_enabled=False)
    prefix = a.canonical_prefix()
    parsed = json.loads(prefix)  # must be valid JSON
    assert set(parsed.keys()) == {"system", "tools"}
    # sort_keys means re-dumping with the same options is a fixed point
    redump = json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert redump == prefix


@pytest.mark.mocked
def test_no_skill_router_tool_names_and_order():
    a = PromptAssembler(skill_router=None, codegraph_enabled=False)
    names = [t["function"]["name"] for t in a.tools()]
    assert names == ["read_file", "write_file", "list_dir", "run_bash", "run_tests", "finish"]


@pytest.mark.mocked
def test_skill_router_prepends_load_skill_and_call_skill_tool():
    runner = MagicMock()
    runner.tool_schema.return_value = {
        "type": "function",
        "function": {"name": "load_skill", "parameters": {}},
    }
    runner.catalog_prompt.return_value = "- test-skill: A test skill"
    sr = MagicMock()
    sr.runner = runner
    a = PromptAssembler(skill_router=sr, codegraph_enabled=False)
    names = [t["function"]["name"] for t in a.tools()]
    assert names == [
        "load_skill",
        "call_skill_tool",
        "read_file",
        "write_file",
        "list_dir",
        "run_bash",
        "run_tests",
        "finish",
    ]
    # catalog is appended to the system content (progressive disclosure)
    assert "test-skill" in a.system_message()["content"]


@pytest.mark.mocked
def test_codegraph_enabled_inserts_semantic_search_before_static_tail():
    a = PromptAssembler(skill_router=None, codegraph_enabled=True)
    names = [t["function"]["name"] for t in a.tools()]
    assert names == [
        "code_semantic_search",
        "read_file",
        "write_file",
        "list_dir",
        "run_bash",
        "run_tests",
        "finish",
    ]


@pytest.mark.mocked
def test_base_system_prompt_directs_code_to_sandbox():
    # Mirrors test_react_system_prompt_directs_code_to_sandbox parity assertions.
    a = PromptAssembler(skill_router=None)
    content = a.system_message()["content"]
    assert "code-sandbox" in content
    assert "run_bash" in content


@pytest.mark.mocked
def test_no_nondeterministic_tokens_in_prefix():
    # Guard: the canonical prefix must contain no time/uuid/random markers.
    import re

    a = PromptAssembler(skill_router=None, codegraph_enabled=True)
    prefix = a.canonical_prefix()
    # ISO timestamp fragment, e.g. 2026-06-25T or a 32-hex uuid would be a leak.
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", prefix)
    assert not re.search(r"[0-9a-f]{32}", prefix)


@pytest.mark.mocked
def test_memory_disabled_by_default_no_memory_search_tool():
    a = PromptAssembler(skill_router=None)
    names = [t["function"]["name"] for t in a.tools()]
    assert "memory_search" not in names


@pytest.mark.mocked
def test_memory_enabled_inserts_memory_search_before_static_tail():
    a = PromptAssembler(skill_router=None, codegraph_enabled=False, memory_enabled=True)
    names = [t["function"]["name"] for t in a.tools()]
    assert names == [
        "memory_search",
        "read_file",
        "write_file",
        "list_dir",
        "run_bash",
        "run_tests",
        "finish",
    ]


@pytest.mark.mocked
def test_memory_search_after_code_semantic_search_when_both_enabled():
    a = PromptAssembler(skill_router=None, codegraph_enabled=True, memory_enabled=True)
    names = [t["function"]["name"] for t in a.tools()]
    assert names == [
        "code_semantic_search",
        "memory_search",
        "read_file",
        "write_file",
        "list_dir",
        "run_bash",
        "run_tests",
        "finish",
    ]


@pytest.mark.mocked
def test_memory_search_schema_params():
    a = PromptAssembler(skill_router=None, memory_enabled=True)
    schema = next(t for t in a.tools() if t["function"]["name"] == "memory_search")
    props = schema["function"]["parameters"]["properties"]
    assert "query" in props
    assert "k" in props
    assert schema["function"]["parameters"]["required"] == ["query"]


@pytest.mark.mocked
def test_base_system_prompt_mentions_memory_search():
    a = PromptAssembler(skill_router=None, memory_enabled=True)
    assert "memory_search" in a.system_message()["content"]


@pytest.mark.mocked
def test_memory_enabled_prefix_is_deterministic_and_clean():
    import re

    a = PromptAssembler(skill_router=None, codegraph_enabled=True, memory_enabled=True)
    b = PromptAssembler(skill_router=None, codegraph_enabled=True, memory_enabled=True)
    assert a.prefix_fingerprint() == b.prefix_fingerprint()
    prefix = a.canonical_prefix()
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", prefix)
    assert not re.search(r"[0-9a-f]{32}", prefix)


@pytest.mark.mocked
def test_base_prompt_names_code_sandbox_tools():
    for name in ("run_python", "run_shell", "run_tests", "install_packages"):
        assert name in BASE_SYSTEM_PROMPT
    # named together so the model uses them verbatim
    assert "code-sandbox" in BASE_SYSTEM_PROMPT


@pytest.mark.mocked
def test_no_client_manifest_no_steer_prefix_unchanged():
    """When client_manifest is None, the system message equals the pre-change content."""
    a = PromptAssembler(skill_router=None, codegraph_enabled=False, client_manifest=None)
    content = a.system_message()["content"]
    # Should equal BASE_SYSTEM_PROMPT exactly (no client steer).
    assert content == BASE_SYSTEM_PROMPT


@pytest.mark.mocked
def test_no_client_prefix_byte_identical_no_vs_explicit_none():
    """PromptAssembler() and PromptAssembler(client_manifest=None) produce identical prefixes."""
    a = PromptAssembler(skill_router=None, codegraph_enabled=False)
    b = PromptAssembler(skill_router=None, codegraph_enabled=False, client_manifest=None)
    assert a.canonical_prefix() == b.canonical_prefix()
    assert a.prefix_fingerprint() == b.prefix_fingerprint()


@pytest.mark.mocked
def test_client_manifest_appends_steer_clause():
    """When client_manifest is not None, the steer clause is appended to system_text."""
    from services.orchestrator.prompt_assembler import CLIENT_PRIMITIVES_STEER
    from services.orchestrator.tool_manifest import parse_manifest

    manifest = parse_manifest({"tools": [{"name": "search_files", "source": "builtin"}]})
    a = PromptAssembler(skill_router=None, codegraph_enabled=False, client_manifest=manifest)
    content = a.system_message()["content"]

    # Should contain the base prompt.
    assert BASE_SYSTEM_PROMPT in content
    # Should contain the steer clause.
    assert CLIENT_PRIMITIVES_STEER in content
    # Steer should come after base (append, not replace).
    assert content.find(BASE_SYSTEM_PROMPT) < content.find(CLIENT_PRIMITIVES_STEER)


@pytest.mark.mocked
def test_client_manifest_prefix_stable_across_instances():
    """Two assemblers with the same manifest produce identical prefixes (canonical_prefix)."""
    from services.orchestrator.tool_manifest import parse_manifest

    manifest = parse_manifest({"tools": [{"name": "read_file", "source": "builtin"}]})
    a = PromptAssembler(skill_router=None, codegraph_enabled=False, client_manifest=manifest)
    b = PromptAssembler(skill_router=None, codegraph_enabled=False, client_manifest=manifest)

    assert a.canonical_prefix() == b.canonical_prefix()
    assert a.prefix_fingerprint() == b.prefix_fingerprint()
