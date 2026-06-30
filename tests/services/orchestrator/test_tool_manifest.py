"""
Tests for client tool-manifest contract and build_tool_list logic.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from services.orchestrator.prompt_assembler import (
    PromptAssembler,
    _static_tail_schemas,
)
from services.orchestrator.tool_manifest import (
    CANONICAL_BUILTIN_SCHEMAS,
    ClientManifest,
    build_tool_list,
    parse_manifest,
)


@pytest.mark.mocked
def test_parse_manifest_none_input():
    """parse_manifest(None) returns None."""
    assert parse_manifest(None) is None


@pytest.mark.mocked
def test_parse_manifest_empty_dict():
    """parse_manifest({}) returns None."""
    assert parse_manifest({}) is None


@pytest.mark.mocked
def test_parse_manifest_empty_tools_list():
    """parse_manifest({"tools": []}) returns None."""
    assert parse_manifest({"tools": []}) is None


@pytest.mark.mocked
def test_parse_manifest_single_builtin_tool_camelcase():
    """parse_manifest accepts camelCase protocolVersion."""
    payload = {"protocolVersion": 1, "tools": [{"name": "read_file", "source": "builtin"}]}
    manifest = parse_manifest(payload)
    assert manifest is not None
    assert manifest["protocol_version"] == 1
    assert len(manifest["tools"]) == 1
    assert manifest["tools"][0]["name"] == "read_file"
    assert manifest["tools"][0]["source"] == "builtin"


@pytest.mark.mocked
def test_parse_manifest_snake_case_protocol_version():
    """parse_manifest accepts snake_case protocol_version."""
    payload = {"protocol_version": 1, "tools": [{"name": "write_file"}]}
    manifest = parse_manifest(payload)
    assert manifest is not None
    assert manifest["protocol_version"] == 1


@pytest.mark.mocked
def test_parse_manifest_defaults_source_to_builtin():
    """parse_manifest defaults source to 'builtin' if omitted."""
    payload = {"tools": [{"name": "list_dir"}]}
    manifest = parse_manifest(payload)
    assert manifest is not None
    assert manifest["tools"][0]["source"] == "builtin"


@pytest.mark.mocked
def test_parse_manifest_skips_malformed_tools():
    """parse_manifest skips tools with no name."""
    payload = {
        "tools": [
            {"name": "read_file"},
            {"source": "builtin"},  # missing name
            {"name": "write_file"},
        ]
    }
    manifest = parse_manifest(payload)
    assert manifest is not None
    assert len(manifest["tools"]) == 2
    assert [t["name"] for t in manifest["tools"]] == ["read_file", "write_file"]


@pytest.mark.mocked
def test_parse_manifest_preserves_optional_fields():
    """parse_manifest preserves namespace and schema when present."""
    schema_obj = {"type": "function", "function": {"name": "my_tool"}}
    payload = {
        "tools": [
            {
                "name": "my_tool",
                "source": "mcp",
                "namespace": "my_server",
                "schema": schema_obj,
            }
        ]
    }
    manifest = parse_manifest(payload)
    assert manifest is not None
    tool = manifest["tools"][0]
    assert tool["namespace"] == "my_server"
    assert tool["schema"] == schema_obj


@pytest.mark.mocked
def test_parse_manifest_ignores_unknown_fields():
    """parse_manifest tolerates and ignores unknown fields."""
    payload = {
        "protocolVersion": 1,
        "tools": [{"name": "read_file", "unknown_field": "should_be_ignored"}],
        "other_toplevel": "ignored",
    }
    manifest = parse_manifest(payload)
    assert manifest is not None
    # Should not have the unknown field in the descriptor
    assert "unknown_field" not in manifest["tools"][0]


@pytest.mark.mocked
def test_canonical_builtin_schemas_contains_expected_keys():
    """CANONICAL_BUILTIN_SCHEMAS has all required builtins."""
    expected = {"read_file", "write_file", "list_dir", "search_files", "run_tests"}
    assert set(CANONICAL_BUILTIN_SCHEMAS.keys()) == expected


@pytest.mark.mocked
def test_canonical_builtin_schemas_byte_identical_to_prompt_assembler():
    """read_file, write_file, list_dir, run_tests schemas match prompt_assembler exactly."""
    static_tail = _static_tail_schemas()
    for schema in static_tail:
        name = schema["function"]["name"]
        if name in CANONICAL_BUILTIN_SCHEMAS:
            # Byte-identical (schema and all properties match).
            assert CANONICAL_BUILTIN_SCHEMAS[name] == schema, f"Drift in {name}"


@pytest.mark.mocked
def test_build_tool_list_no_manifest_fallback_no_skills():
    """build_tool_list(manifest=None) returns fallback: no skills, no codegraph."""
    static_tail = _static_tail_schemas()
    tools = build_tool_list(
        None, skill_router=None, codegraph_enabled=False, static_tail=static_tail
    )
    names = [t["function"]["name"] for t in tools]
    assert names == ["read_file", "write_file", "list_dir", "run_bash", "run_tests", "finish"]


@pytest.mark.mocked
def test_build_tool_list_no_manifest_fallback_with_skills():
    """build_tool_list(manifest=None) with skill_router includes load_skill and call_skill_tool."""
    runner = MagicMock()
    runner.tool_schema.return_value = {
        "type": "function",
        "function": {"name": "load_skill", "parameters": {}},
    }
    sr = MagicMock()
    sr.runner = runner
    static_tail = _static_tail_schemas()
    tools = build_tool_list(None, skill_router=sr, codegraph_enabled=False, static_tail=static_tail)
    names = [t["function"]["name"] for t in tools]
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


@pytest.mark.mocked
def test_build_tool_list_no_manifest_fallback_with_codegraph():
    """build_tool_list(manifest=None, codegraph_enabled=True) includes code_semantic_search."""
    static_tail = _static_tail_schemas()
    tools = build_tool_list(
        None, skill_router=None, codegraph_enabled=True, static_tail=static_tail
    )
    names = [t["function"]["name"] for t in tools]
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
def test_build_tool_list_no_manifest_fallback_with_memory():
    """build_tool_list(manifest=None, memory_enabled=True) includes memory_search."""
    static_tail = _static_tail_schemas()
    tools = build_tool_list(
        None,
        skill_router=None,
        codegraph_enabled=False,
        memory_enabled=True,
        static_tail=static_tail,
    )
    names = [t["function"]["name"] for t in tools]
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
def test_build_tool_list_client_attached_no_run_bash():
    """build_tool_list with client manifest never advertises run_bash."""
    manifest: ClientManifest = {
        "tools": [
            {"name": "read_file", "source": "builtin"},
            {"name": "write_file", "source": "builtin"},
            {"name": "list_dir", "source": "builtin"},
        ]
    }
    static_tail = _static_tail_schemas()
    tools = build_tool_list(
        manifest, skill_router=None, codegraph_enabled=False, static_tail=static_tail
    )
    names = [t["function"]["name"] for t in tools]
    assert "run_bash" not in names
    assert names == ["read_file", "write_file", "list_dir", "finish"]


@pytest.mark.mocked
def test_build_tool_list_client_attached_advertises_declared_builtins():
    """build_tool_list advertises only declared builtins in fixed order."""
    manifest: ClientManifest = {
        "tools": [
            {"name": "write_file", "source": "builtin"},
            {"name": "read_file", "source": "builtin"},
            {"name": "run_tests", "source": "builtin"},
        ]
    }
    static_tail = _static_tail_schemas()
    tools = build_tool_list(
        manifest, skill_router=None, codegraph_enabled=False, static_tail=static_tail
    )
    names = [t["function"]["name"] for t in tools]
    # Should be in canonical order (read, write, list, search, run_tests), not manifest order.
    assert names == ["read_file", "write_file", "run_tests", "finish"]


@pytest.mark.mocked
def test_build_tool_list_client_attached_skips_undeclared_builtins():
    """build_tool_list skips builtins not in the manifest."""
    manifest: ClientManifest = {
        "tools": [
            {"name": "read_file", "source": "builtin"},
        ]
    }
    static_tail = _static_tail_schemas()
    tools = build_tool_list(
        manifest, skill_router=None, codegraph_enabled=False, static_tail=static_tail
    )
    names = [t["function"]["name"] for t in tools]
    assert names == ["read_file", "finish"]


@pytest.mark.mocked
def test_build_tool_list_client_attached_search_files():
    """build_tool_list advertises search_files when declared."""
    manifest: ClientManifest = {
        "tools": [
            {"name": "search_files", "source": "builtin"},
        ]
    }
    static_tail = _static_tail_schemas()
    tools = build_tool_list(
        manifest, skill_router=None, codegraph_enabled=False, static_tail=static_tail
    )
    names = [t["function"]["name"] for t in tools]
    assert "search_files" in names
    # Verify the schema is canonical.
    schema = next(t for t in tools if t["function"]["name"] == "search_files")
    assert schema == CANONICAL_BUILTIN_SCHEMAS["search_files"]


@pytest.mark.mocked
def test_build_tool_list_client_attached_code_semantic_search_only_if_declared():
    """build_tool_list includes code_semantic_search only if declared in manifest."""
    manifest: ClientManifest = {
        "tools": [
            {"name": "read_file", "source": "builtin"},
        ]
    }
    static_tail = _static_tail_schemas()
    # codegraph_enabled=True but not declared in manifest → should NOT appear.
    tools = build_tool_list(
        manifest, skill_router=None, codegraph_enabled=True, static_tail=static_tail
    )
    names = [t["function"]["name"] for t in tools]
    assert "code_semantic_search" not in names


@pytest.mark.mocked
def test_build_tool_list_client_attached_code_semantic_search_declared():
    """build_tool_list includes code_semantic_search if declared in manifest."""
    manifest: ClientManifest = {
        "tools": [
            {"name": "read_file", "source": "builtin"},
            {"name": "code_semantic_search", "source": "builtin"},
        ]
    }
    static_tail = _static_tail_schemas()
    tools = build_tool_list(
        manifest, skill_router=None, codegraph_enabled=False, static_tail=static_tail
    )
    names = [t["function"]["name"] for t in tools]
    assert "code_semantic_search" in names


@pytest.mark.mocked
def test_build_tool_list_client_attached_mcp_tools_namespaced():
    """build_tool_list namespaces mcp tools and sorts them."""
    schema1 = {
        "type": "function",
        "function": {"name": "tool_a", "description": "Tool A", "parameters": {}},
    }
    schema2 = {
        "type": "function",
        "function": {"name": "tool_b", "description": "Tool B", "parameters": {}},
    }
    manifest: ClientManifest = {
        "tools": [
            {"name": "tool_a", "source": "mcp", "namespace": "server1", "schema": schema1},
            {"name": "tool_b", "source": "mcp", "namespace": "server2", "schema": schema2},
        ]
    }
    static_tail = _static_tail_schemas()
    tools = build_tool_list(
        manifest, skill_router=None, codegraph_enabled=False, static_tail=static_tail
    )
    names = [t["function"]["name"] for t in tools]
    # Should be sorted by final name.
    assert "mcp__server1__tool_a" in names
    assert "mcp__server2__tool_b" in names
    # mcp__server1__tool_a comes before mcp__server2__tool_b alphabetically.
    idx_a = names.index("mcp__server1__tool_a")
    idx_b = names.index("mcp__server2__tool_b")
    assert idx_a < idx_b


@pytest.mark.mocked
def test_build_tool_list_client_attached_mcp_tools_no_namespace():
    """build_tool_list mcp tools without namespace are not prefixed."""
    schema = {
        "type": "function",
        "function": {"name": "my_tool", "description": "My Tool", "parameters": {}},
    }
    manifest: ClientManifest = {
        "tools": [
            {"name": "my_tool", "source": "mcp", "schema": schema},
        ]
    }
    static_tail = _static_tail_schemas()
    tools = build_tool_list(
        manifest, skill_router=None, codegraph_enabled=False, static_tail=static_tail
    )
    names = [t["function"]["name"] for t in tools]
    # Without a namespace, the tool name should be as-is.
    assert "my_tool" in names


@pytest.mark.mocked
def test_build_tool_list_client_attached_mcp_tools_skipped_without_schema():
    """build_tool_list skips mcp/skill tools that don't carry a schema."""
    manifest: ClientManifest = {
        "tools": [
            {"name": "toolx", "source": "mcp", "namespace": "srv1"},  # no schema
        ]
    }
    static_tail = _static_tail_schemas()
    tools = build_tool_list(
        manifest, skill_router=None, codegraph_enabled=False, static_tail=static_tail
    )
    names = [t["function"]["name"] for t in tools]
    assert "mcp__srv1__toolx" not in names


@pytest.mark.mocked
def test_build_tool_list_finish_always_last():
    """build_tool_list always places finish as the last tool."""
    manifest: ClientManifest = {
        "tools": [
            {"name": "read_file", "source": "builtin"},
            {"name": "write_file", "source": "builtin"},
        ]
    }
    static_tail = _static_tail_schemas()
    tools = build_tool_list(
        manifest, skill_router=None, codegraph_enabled=False, static_tail=static_tail
    )
    names = [t["function"]["name"] for t in tools]
    assert names[-1] == "finish"


@pytest.mark.mocked
def test_build_tool_list_client_attached_skill_router():
    """build_tool_list with client manifest and skill_router includes skills first."""
    runner = MagicMock()
    runner.tool_schema.return_value = {
        "type": "function",
        "function": {"name": "load_skill", "parameters": {}},
    }
    sr = MagicMock()
    sr.runner = runner
    manifest: ClientManifest = {
        "tools": [
            {"name": "read_file", "source": "builtin"},
        ]
    }
    static_tail = _static_tail_schemas()
    tools = build_tool_list(
        manifest, skill_router=sr, codegraph_enabled=False, static_tail=static_tail
    )
    names = [t["function"]["name"] for t in tools]
    assert names == ["load_skill", "call_skill_tool", "read_file", "finish"]


@pytest.mark.mocked
def test_prompt_assembler_with_manifest_none_unchanged():
    """PromptAssembler(client_manifest=None) behaves identically to no manifest param."""
    a = PromptAssembler(skill_router=None, codegraph_enabled=False)
    b = PromptAssembler(skill_router=None, codegraph_enabled=False, client_manifest=None)
    assert a.canonical_prefix() == b.canonical_prefix()
    assert a.prefix_fingerprint() == b.prefix_fingerprint()


@pytest.mark.mocked
def test_prompt_assembler_with_manifest_byte_stable():
    """PromptAssembler instances with the same manifest produce identical prefixes."""
    manifest: ClientManifest = {
        "tools": [
            {"name": "read_file", "source": "builtin"},
            {"name": "write_file", "source": "builtin"},
        ]
    }
    a = PromptAssembler(skill_router=None, codegraph_enabled=False, client_manifest=manifest)
    b = PromptAssembler(skill_router=None, codegraph_enabled=False, client_manifest=manifest)
    assert a.canonical_prefix() == b.canonical_prefix()
    assert a.prefix_fingerprint() == b.prefix_fingerprint()


@pytest.mark.mocked
def test_prompt_assembler_with_manifest_drops_run_bash():
    """PromptAssembler with client manifest never includes run_bash."""
    manifest: ClientManifest = {
        "tools": [
            {"name": "read_file", "source": "builtin"},
            {"name": "write_file", "source": "builtin"},
        ]
    }
    a = PromptAssembler(skill_router=None, codegraph_enabled=False, client_manifest=manifest)
    names = [t["function"]["name"] for t in a.tools()]
    assert "run_bash" not in names


@pytest.mark.mocked
def test_prompt_assembler_existing_tests_still_pass():
    """Existing PromptAssembler tests (no manifest) must still pass."""
    # Test from test_prompt_assembler.py: no_skill_router_tool_names_and_order
    a = PromptAssembler(skill_router=None, codegraph_enabled=False)
    names = [t["function"]["name"] for t in a.tools()]
    assert names == ["read_file", "write_file", "list_dir", "run_bash", "run_tests", "finish"]
