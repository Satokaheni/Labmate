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


@pytest.mark.mocked
def test_workspace_root_without_client_manifest_not_included():
    """When client_manifest is None, workspace_root is ignored (no-client prefix unchanged)."""
    a = PromptAssembler(
        skill_router=None,
        codegraph_enabled=False,
        client_manifest=None,
        workspace_root="/abs/path",
    )
    content = a.system_message()["content"]
    # Should equal BASE_SYSTEM_PROMPT exactly (workspace_root ignored, no manifest).
    assert content == BASE_SYSTEM_PROMPT
    assert "/abs/path" not in content


@pytest.mark.mocked
def test_workspace_root_without_client_manifest_prefix_unchanged():
    """Passing workspace_root without client_manifest does not change the prefix."""
    a = PromptAssembler(skill_router=None, codegraph_enabled=False, client_manifest=None)
    b = PromptAssembler(
        skill_router=None,
        codegraph_enabled=False,
        client_manifest=None,
        workspace_root="/abs/path",
    )
    # Prefixes should be identical: workspace_root is ignored when no manifest.
    assert a.canonical_prefix() == b.canonical_prefix()
    assert a.prefix_fingerprint() == b.prefix_fingerprint()


@pytest.mark.mocked
def test_workspace_root_with_client_manifest_appends_clause():
    """When client_manifest and workspace_root are both set, the workspace root clause is appended."""
    from services.orchestrator.tool_manifest import parse_manifest

    manifest = parse_manifest({"tools": [{"name": "read_file", "source": "builtin"}]})
    a = PromptAssembler(
        skill_router=None,
        codegraph_enabled=False,
        client_manifest=manifest,
        workspace_root="/Users/zach/Work/myproject",
    )
    content = a.system_message()["content"]

    # Should contain the base prompt.
    assert BASE_SYSTEM_PROMPT in content
    # Should contain the workspace root path.
    assert "/Users/zach/Work/myproject" in content
    # Should mention absolute paths and joining with workspace-relative paths.
    assert "ABSOLUTE path" in content
    assert "workspace-relative path" in content
    # Clause should come after both base and steer.
    assert content.find(BASE_SYSTEM_PROMPT) < content.find("/Users/zach/Work/myproject")


@pytest.mark.mocked
def test_workspace_root_stable_across_instances():
    """Two assemblers with same manifest and workspace_root produce identical prefixes."""
    from services.orchestrator.tool_manifest import parse_manifest

    manifest = parse_manifest({"tools": [{"name": "read_file", "source": "builtin"}]})
    ws_root = "/home/user/project"
    a = PromptAssembler(
        skill_router=None,
        codegraph_enabled=False,
        client_manifest=manifest,
        workspace_root=ws_root,
    )
    b = PromptAssembler(
        skill_router=None,
        codegraph_enabled=False,
        client_manifest=manifest,
        workspace_root=ws_root,
    )

    assert a.canonical_prefix() == b.canonical_prefix()
    assert a.prefix_fingerprint() == b.prefix_fingerprint()


@pytest.mark.mocked
def test_workspace_root_empty_string_no_clause_appended():
    """When workspace_root is empty string, the workspace clause is not appended."""
    from services.orchestrator.tool_manifest import parse_manifest

    manifest = parse_manifest({"tools": [{"name": "read_file", "source": "builtin"}]})
    a = PromptAssembler(
        skill_router=None,
        codegraph_enabled=False,
        client_manifest=manifest,
        workspace_root="",
    )
    b = PromptAssembler(
        skill_router=None,
        codegraph_enabled=False,
        client_manifest=manifest,
        workspace_root=None,
    )
    # Both should have the same prefix: empty string and None are equivalent (both falsy).
    assert a.canonical_prefix() == b.canonical_prefix()


@pytest.mark.mocked
def test_workspace_root_different_paths_produce_different_prefixes():
    """Two assemblers with different workspace_root values produce different prefixes."""
    from services.orchestrator.tool_manifest import parse_manifest

    manifest = parse_manifest({"tools": [{"name": "read_file", "source": "builtin"}]})
    a = PromptAssembler(
        skill_router=None,
        codegraph_enabled=False,
        client_manifest=manifest,
        workspace_root="/path/one",
    )
    b = PromptAssembler(
        skill_router=None,
        codegraph_enabled=False,
        client_manifest=manifest,
        workspace_root="/path/two",
    )

    # Prefixes should differ because the workspace roots differ.
    assert a.canonical_prefix() != b.canonical_prefix()
    assert a.prefix_fingerprint() != b.prefix_fingerprint()


# ---------------------------------------------------------------------------
# AGENTS.md — agent_instructions param
# ---------------------------------------------------------------------------


@pytest.mark.mocked
def test_agent_instructions_appear_in_system_message():
    """PromptAssembler(agent_instructions=...) places the text in system_message()['content']."""
    instructions = "Follow the house style."
    a = PromptAssembler(skill_router=None, codegraph_enabled=False, agent_instructions=instructions)
    content = a.system_message()["content"]
    assert instructions in content
    # The section header is also present.
    assert "Project instructions (AGENTS.md)" in content


@pytest.mark.mocked
def test_agent_instructions_in_canonical_prefix():
    """agent_instructions text is part of canonical_prefix (and hence prefix_fingerprint)."""
    instructions = "Follow the house style."
    a = PromptAssembler(skill_router=None, codegraph_enabled=False, agent_instructions=instructions)
    assert instructions in a.canonical_prefix()


@pytest.mark.mocked
def test_agent_instructions_cache_stability():
    """Two assemblers built with identical agent_instructions produce identical fingerprints."""
    instructions = "Always write type annotations."
    a = PromptAssembler(skill_router=None, codegraph_enabled=False, agent_instructions=instructions)
    b = PromptAssembler(skill_router=None, codegraph_enabled=False, agent_instructions=instructions)
    assert a.prefix_fingerprint() == b.prefix_fingerprint()
    assert a.canonical_prefix() == b.canonical_prefix()


@pytest.mark.mocked
def test_agent_instructions_changes_fingerprint():
    """An assembler WITH agent_instructions has a DIFFERENT fingerprint than one without."""
    with_instructions = PromptAssembler(
        skill_router=None, codegraph_enabled=False, agent_instructions="Some project rules."
    )
    without_instructions = PromptAssembler(skill_router=None, codegraph_enabled=False)
    assert with_instructions.prefix_fingerprint() != without_instructions.prefix_fingerprint()


@pytest.mark.mocked
def test_agent_instructions_empty_string_byte_identical_to_no_arg():
    """PromptAssembler(agent_instructions='') is byte-identical to PromptAssembler() — backward compat."""
    a = PromptAssembler(skill_router=None, codegraph_enabled=False)
    b = PromptAssembler(skill_router=None, codegraph_enabled=False, agent_instructions="")
    assert a.system_message()["content"] == b.system_message()["content"]
    assert a.canonical_prefix() == b.canonical_prefix()
    assert a.prefix_fingerprint() == b.prefix_fingerprint()


@pytest.mark.mocked
def test_agent_instructions_whitespace_only_byte_identical_to_no_arg():
    """Whitespace-only agent_instructions is treated as empty — byte-identical prefix."""
    a = PromptAssembler(skill_router=None, codegraph_enabled=False)
    b = PromptAssembler(skill_router=None, codegraph_enabled=False, agent_instructions="   \n  ")
    assert a.system_message()["content"] == b.system_message()["content"]
    assert a.canonical_prefix() == b.canonical_prefix()
    assert a.prefix_fingerprint() == b.prefix_fingerprint()


@pytest.mark.mocked
def test_agent_instructions_appears_after_base_prompt():
    """The AGENTS.md section is appended after BASE_SYSTEM_PROMPT, not before it."""
    instructions = "Prefer functional patterns."
    a = PromptAssembler(skill_router=None, codegraph_enabled=False, agent_instructions=instructions)
    content = a.system_message()["content"]
    assert content.find(BASE_SYSTEM_PROMPT) < content.find(instructions)
