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
    client_doc_skills,
    hosted_skill_namespaces,
    manifest_local_tool_names,
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
def test_build_tool_list_client_attached_mcp_tools_skipped_with_malformed_schema():
    """build_tool_list skips mcp/skill tools with a schema missing 'function' key."""
    manifest: ClientManifest = {
        "tools": [
            {
                "name": "toolx",
                "source": "mcp",
                "namespace": "srv1",
                "schema": {"type": "function"},  # missing 'function' key
            },
        ]
    }
    static_tail = _static_tail_schemas()
    tools = build_tool_list(
        manifest, skill_router=None, codegraph_enabled=False, static_tail=static_tail
    )
    names = [t["function"]["name"] for t in tools]
    assert "mcp__srv1__toolx" not in names


@pytest.mark.mocked
def test_build_tool_list_client_attached_mcp_tools_accepted_with_valid_schema():
    """build_tool_list advertises mcp/skill tools with a valid schema (dict with 'function')."""
    schema = {
        "type": "function",
        "function": {"name": "toolx", "description": "Tool X", "parameters": {}},
    }
    manifest: ClientManifest = {
        "tools": [
            {
                "name": "toolx",
                "source": "mcp",
                "namespace": "srv1",
                "schema": schema,
            },
        ]
    }
    static_tail = _static_tail_schemas()
    tools = build_tool_list(
        manifest, skill_router=None, codegraph_enabled=False, static_tail=static_tail
    )
    names = [t["function"]["name"] for t in tools]
    assert "mcp__srv1__toolx" in names


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

    # Mock tool_schema to return a schema with non-empty enum so load_skill is included.
    def mock_tool_schema(exclude=frozenset()):
        enum_skills = ["code-review", "test-gen"]
        enum_skills = [s for s in enum_skills if s not in exclude]
        return {
            "type": "function",
            "function": {
                "name": "load_skill",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "enum": enum_skills,
                        }
                    },
                    "required": ["name"],
                },
            },
        }

    runner.tool_schema = mock_tool_schema
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


@pytest.mark.mocked
def test_manifest_local_tool_names_no_manifest():
    """manifest_local_tool_names(None, fallback) returns the fallback set."""
    fallback = {"read_file", "write_file", "list_dir"}
    result = manifest_local_tool_names(None, fallback)
    assert result == fallback


@pytest.mark.mocked
def test_manifest_local_tool_names_builtin_tools():
    """manifest_local_tool_names extracts builtin tool names from manifest."""
    manifest: ClientManifest = {
        "tools": [
            {"name": "read_file", "source": "builtin"},
            {"name": "write_file", "source": "builtin"},
            {"name": "search_files", "source": "builtin"},
        ]
    }
    result = manifest_local_tool_names(manifest, set())
    assert result == {"read_file", "write_file", "search_files"}


@pytest.mark.mocked
def test_manifest_local_tool_names_mcp_tool_with_namespace():
    """manifest_local_tool_names namespaces mcp tools correctly."""
    manifest: ClientManifest = {
        "tools": [
            {"name": "read_file", "source": "builtin"},
            {
                "name": "foo",
                "source": "mcp",
                "namespace": "srv",
                "schema": {"type": "function", "function": {"name": "foo"}},
            },
        ]
    }
    result = manifest_local_tool_names(manifest, set())
    assert result == {"read_file", "mcp__srv__foo"}


@pytest.mark.mocked
def test_manifest_local_tool_names_mcp_tool_no_namespace():
    """manifest_local_tool_names uses tool name as-is when namespace is absent."""
    manifest: ClientManifest = {
        "tools": [
            {
                "name": "my_tool",
                "source": "mcp",
                "schema": {"type": "function", "function": {"name": "my_tool"}},
            },
        ]
    }
    result = manifest_local_tool_names(manifest, set())
    assert result == {"my_tool"}


@pytest.mark.mocked
def test_manifest_local_tool_names_skips_entries_without_name():
    """manifest_local_tool_names skips descriptors without a name field."""
    manifest: ClientManifest = {
        "tools": [
            {"name": "read_file", "source": "builtin"},
            {"source": "mcp", "namespace": "srv"},  # no name
            {"name": "write_file", "source": "builtin"},
        ]
    }
    result = manifest_local_tool_names(manifest, set())
    assert result == {"read_file", "write_file"}


@pytest.mark.mocked
def test_manifest_local_tool_names_matches_advertised_names():
    """manifest_local_tool_names output matches advertised function names from build_tool_list.

    This sync guard ensures dispatch routing cannot drift from advertisement.
    The set should equal all advertised tool names EXCEPT control/server tools
    (finish, load_skill, call_skill_tool, code_semantic_search, memory_search).

    Strengthened: now includes a skill_router so load_skill and call_skill_tool are
    advertised, making the control_tools subtraction load-bearing and catching regressions
    where a control tool leaks into manifest_local_tool_names.
    """
    # Build a fake skill_router so load_skill and call_skill_tool are advertised.
    runner = MagicMock()

    # Mock tool_schema to return a schema with non-empty enum so load_skill is included.
    def mock_tool_schema(exclude=frozenset()):
        enum_skills = ["code-review", "test-gen"]
        enum_skills = [s for s in enum_skills if s not in exclude]
        return {
            "type": "function",
            "function": {
                "name": "load_skill",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "enum": enum_skills,
                        }
                    },
                    "required": ["name"],
                },
            },
        }

    runner.tool_schema = mock_tool_schema
    runner.catalog_prompt.return_value = None
    fake_router = MagicMock()
    fake_router.runner = runner

    manifest: ClientManifest = {
        "tools": [
            {"name": "read_file", "source": "builtin"},
            {"name": "write_file", "source": "builtin"},
            {"name": "search_files", "source": "builtin"},
            {
                "name": "my_mcp_tool",
                "source": "mcp",
                "namespace": "my_server",
                "schema": {
                    "type": "function",
                    "function": {"name": "my_mcp_tool", "parameters": {}},
                },
            },
        ]
    }
    static_tail = _static_tail_schemas()
    advertised_tools = build_tool_list(
        manifest, skill_router=fake_router, codegraph_enabled=False, static_tail=static_tail
    )
    advertised_names = {t["function"]["name"] for t in advertised_tools}

    # Get the local tool names.
    local_names = manifest_local_tool_names(manifest, set())

    # Control/server tools that should NOT be in local_names.
    # These are explicitly managed by the orchestrator and should not be routed
    # to the client even if they appear in the advertised tool list.
    control_tools = {
        "finish",
        "load_skill",
        "call_skill_tool",
        "code_semantic_search",
        "memory_search",
    }

    # Compute expected: advertised minus control tools.
    expected = advertised_names - control_tools

    # They must match.
    assert local_names == expected, f"Expected {expected}, got {local_names}"
    # Sanity check: control_tools should actually be in advertised (otherwise the
    # subtraction is vacuous and the test is weak). With a fake_router, load_skill
    # and call_skill_tool should definitely be present.
    assert {"load_skill", "call_skill_tool"} & advertised_names, (
        "load_skill and/or call_skill_tool should be in advertised_names for the sync guard to be "
        "load-bearing; otherwise the control_tools subtraction is partially a no-op"
    )


@pytest.mark.mocked
def test_manifest_local_tool_names_dispatch_routing():
    """Test that manifest_local_tool_names correctly identifies tools that should route to the client.

    This is the core routing check used in coding_orchestrator._run_react_loop:
    `elif name in local_tool_names:` dispatches to request_local_tool.

    When a client declares a builtin tool (e.g., search_files) in the manifest,
    it must be in the local_tool_names set so the dispatch branch catches it.
    When the client is not attached (no manifest), fallback to LOCAL_TOOL_NAMES.
    """
    # Case 1: Client attached, declares read_file and search_files.
    manifest: ClientManifest = {
        "tools": [
            {"name": "read_file", "source": "builtin"},
            {"name": "search_files", "source": "builtin"},
        ]
    }
    local_names = manifest_local_tool_names(manifest, {"read_file", "write_file", "list_dir"})
    # Should have exactly what the manifest declares (builtin tools).
    assert "read_file" in local_names
    assert "search_files" in local_names
    # write_file not declared; should NOT be in local_names (manifest is authoritative).
    assert "write_file" not in local_names

    # Case 2: No client (manifest=None), fall back to static LOCAL_TOOL_NAMES.
    fallback = {"read_file", "write_file", "list_dir"}
    local_names = manifest_local_tool_names(None, fallback)
    assert local_names == fallback

    # Case 3: Client declares an MCP tool with namespace and VALID schema.
    # The dispatch check must use the final namespaced name.
    manifest = {
        "tools": [
            {
                "name": "search",
                "source": "mcp",
                "namespace": "fs",
                "schema": {"type": "function", "function": {"name": "search"}},
            },
        ]
    }
    local_names = manifest_local_tool_names(manifest, set())
    assert "mcp__fs__search" in local_names  # Dispatch check looks for this exact name.
    assert "search" not in local_names  # NOT the bare name.


@pytest.mark.mocked
def test_manifest_local_tool_names_skips_mcp_without_schema():
    """manifest_local_tool_names excludes mcp/skill tools without a schema."""
    manifest: ClientManifest = {
        "tools": [
            {"name": "read_file", "source": "builtin"},
            {"name": "toolx", "source": "mcp", "namespace": "srv1"},  # no schema
        ]
    }
    local_names = manifest_local_tool_names(manifest, set())
    assert "read_file" in local_names
    assert "mcp__srv1__toolx" not in local_names


@pytest.mark.mocked
def test_manifest_local_tool_names_skips_mcp_with_malformed_schema():
    """manifest_local_tool_names excludes mcp/skill tools with a schema missing 'function' key."""
    manifest: ClientManifest = {
        "tools": [
            {"name": "read_file", "source": "builtin"},
            {
                "name": "toolx",
                "source": "mcp",
                "namespace": "srv1",
                "schema": {"type": "function"},  # missing 'function' key
            },
        ]
    }
    local_names = manifest_local_tool_names(manifest, set())
    assert "read_file" in local_names
    assert "mcp__srv1__toolx" not in local_names


@pytest.mark.mocked
def test_manifest_local_tool_names_accepts_mcp_with_valid_schema():
    """manifest_local_tool_names includes mcp/skill tools with a valid schema."""
    manifest: ClientManifest = {
        "tools": [
            {"name": "read_file", "source": "builtin"},
            {
                "name": "toolx",
                "source": "mcp",
                "namespace": "srv1",
                "schema": {"type": "function", "function": {"name": "toolx"}},
            },
        ]
    }
    local_names = manifest_local_tool_names(manifest, set())
    assert "read_file" in local_names
    assert "mcp__srv1__toolx" in local_names


@pytest.mark.mocked
def test_hosted_skill_namespaces_none_manifest():
    """hosted_skill_namespaces(None) returns empty set."""
    assert hosted_skill_namespaces(None) == set()


@pytest.mark.mocked
def test_hosted_skill_namespaces_empty_tools():
    """hosted_skill_namespaces with empty tools list returns empty set."""
    manifest: ClientManifest = {"tools": []}
    assert hosted_skill_namespaces(manifest) == set()


@pytest.mark.mocked
def test_hosted_skill_namespaces_mcp_with_namespace():
    """hosted_skill_namespaces extracts mcp/skill tool namespaces."""
    schema = {"type": "function", "function": {"name": "find_references"}}
    manifest: ClientManifest = {
        "tools": [
            {
                "name": "find_references",
                "source": "mcp",
                "namespace": "ast-ts-refactor",
                "schema": schema,
            }
        ]
    }
    assert hosted_skill_namespaces(manifest) == {"ast-ts-refactor"}


@pytest.mark.mocked
def test_hosted_skill_namespaces_skill_with_namespace():
    """hosted_skill_namespaces extracts skill tool namespaces."""
    schema = {"type": "function", "function": {"name": "generate"}}
    manifest: ClientManifest = {
        "tools": [
            {
                "name": "generate",
                "source": "skill",
                "namespace": "code-gen",
                "schema": schema,
            }
        ]
    }
    assert hosted_skill_namespaces(manifest) == {"code-gen"}


@pytest.mark.mocked
def test_hosted_skill_namespaces_multiple():
    """hosted_skill_namespaces extracts multiple namespaces."""
    schema1 = {"type": "function", "function": {"name": "tool1"}}
    schema2 = {"type": "function", "function": {"name": "tool2"}}
    manifest: ClientManifest = {
        "tools": [
            {
                "name": "tool1",
                "source": "mcp",
                "namespace": "ast-ts-refactor",
                "schema": schema1,
            },
            {
                "name": "tool2",
                "source": "mcp",
                "namespace": "code-review",
                "schema": schema2,
            },
        ]
    }
    assert hosted_skill_namespaces(manifest) == {"ast-ts-refactor", "code-review"}


@pytest.mark.mocked
def test_hosted_skill_namespaces_excludes_builtin():
    """hosted_skill_namespaces excludes builtin tools."""
    manifest: ClientManifest = {
        "tools": [
            {"name": "read_file", "source": "builtin"},
        ]
    }
    assert hosted_skill_namespaces(manifest) == set()


@pytest.mark.mocked
def test_hosted_skill_namespaces_excludes_mcp_without_namespace():
    """hosted_skill_namespaces excludes mcp tools without namespace."""
    schema = {"type": "function", "function": {"name": "tool1"}}
    manifest: ClientManifest = {
        "tools": [
            {
                "name": "tool1",
                "source": "mcp",
                "schema": schema,
            }
        ]
    }
    assert hosted_skill_namespaces(manifest) == set()


@pytest.mark.mocked
def test_hosted_skill_namespaces_excludes_mcp_without_schema():
    """hosted_skill_namespaces excludes mcp tools without a schema."""
    manifest: ClientManifest = {
        "tools": [
            {
                "name": "tool1",
                "source": "mcp",
                "namespace": "srv1",
            }
        ]
    }
    assert hosted_skill_namespaces(manifest) == set()


@pytest.mark.mocked
def test_build_tool_list_client_with_hosted_skills_drops_from_enum():
    """build_tool_list drops client-hosted skills from load_skill enum."""
    runner = MagicMock()

    # Mock the tool_schema to track what exclude set was passed.
    def mock_tool_schema(exclude=frozenset()):
        enum_skills = ["code-review", "test-gen"]  # Original full catalog
        enum_skills = [s for s in enum_skills if s not in exclude]
        return {
            "type": "function",
            "function": {
                "name": "load_skill",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "enum": enum_skills,
                        }
                    },
                    "required": ["name"],
                },
            },
        }

    runner.tool_schema = mock_tool_schema
    sr = MagicMock()
    sr.runner = runner

    schema_tool = {
        "type": "function",
        "function": {"name": "find_references", "parameters": {}},
    }
    manifest: ClientManifest = {
        "tools": [
            {
                "name": "find_references",
                "source": "mcp",
                "namespace": "ast-ts-refactor",
                "schema": schema_tool,
            },
            {"name": "read_file", "source": "builtin"},
        ]
    }
    static_tail = _static_tail_schemas()
    tools = build_tool_list(
        manifest, skill_router=sr, codegraph_enabled=False, static_tail=static_tail
    )

    # load_skill should be present; check its enum
    load_skill = next((t for t in tools if t["function"]["name"] == "load_skill"), None)
    assert load_skill is not None
    # The enum should not contain "ast-ts-refactor" (it was excluded)
    enum = load_skill["function"]["parameters"]["properties"]["name"]["enum"]
    assert "ast-ts-refactor" not in enum


@pytest.mark.mocked
def test_build_tool_list_client_all_skills_hosted_omits_load_skill():
    """build_tool_list omits load_skill when all pod skills are client-hosted."""
    runner = MagicMock()

    # All skills are hosted by the client, so exclude set is the full catalog.
    def mock_tool_schema(exclude=frozenset()):
        enum_skills = [s for s in ["code-review", "test-gen"] if s not in exclude]
        return {
            "type": "function",
            "function": {
                "name": "load_skill",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "enum": enum_skills,
                        }
                    },
                    "required": ["name"],
                },
            },
        }

    runner.tool_schema = mock_tool_schema
    sr = MagicMock()
    sr.runner = runner

    # Client hosts all skills
    schema1 = {"type": "function", "function": {"name": "tool1"}}
    schema2 = {"type": "function", "function": {"name": "tool2"}}
    manifest: ClientManifest = {
        "tools": [
            {
                "name": "tool1",
                "source": "mcp",
                "namespace": "code-review",
                "schema": schema1,
            },
            {
                "name": "tool2",
                "source": "mcp",
                "namespace": "test-gen",
                "schema": schema2,
            },
            {"name": "read_file", "source": "builtin"},
        ]
    }
    static_tail = _static_tail_schemas()
    tools = build_tool_list(
        manifest, skill_router=sr, codegraph_enabled=False, static_tail=static_tail
    )
    names = [t["function"]["name"] for t in tools]

    # load_skill and call_skill_tool should NOT be present (empty enum)
    assert "load_skill" not in names
    assert "call_skill_tool" not in names
    # Client MCP tools and finish should be present
    assert "mcp__code-review__tool1" in names
    assert "mcp__test-gen__tool2" in names
    assert "finish" in names


@pytest.mark.mocked
def test_build_tool_list_no_manifest_byte_identical_no_exclude():
    """build_tool_list with manifest=None is byte-identical (no exclude path taken)."""
    static_tail = _static_tail_schemas()

    # Build twice with no manifest; should be identical.
    tools1 = build_tool_list(
        None, skill_router=None, codegraph_enabled=False, static_tail=static_tail
    )
    tools2 = build_tool_list(
        None, skill_router=None, codegraph_enabled=False, static_tail=static_tail
    )

    import json

    assert json.dumps(tools1, sort_keys=True) == json.dumps(tools2, sort_keys=True)


# === Tests for parse_manifest description + body preservation ===


@pytest.mark.mocked
def test_parse_manifest_preserves_description():
    """parse_manifest preserves description field when present."""
    payload = {
        "tools": [
            {
                "name": "repo-tips",
                "source": "skill",
                "description": "Tips for this repo",
            }
        ]
    }
    manifest = parse_manifest(payload)
    assert manifest is not None
    assert manifest["tools"][0]["description"] == "Tips for this repo"


@pytest.mark.mocked
def test_parse_manifest_preserves_body():
    """parse_manifest preserves body field when present."""
    payload = {
        "tools": [
            {
                "name": "repo-tips",
                "source": "skill",
                "body": "This is the skill body content.",
            }
        ]
    }
    manifest = parse_manifest(payload)
    assert manifest is not None
    assert manifest["tools"][0]["body"] == "This is the skill body content."


@pytest.mark.mocked
def test_parse_manifest_omits_missing_description():
    """parse_manifest omits description field when not in payload."""
    payload = {
        "tools": [
            {
                "name": "repo-tips",
                "source": "skill",
            }
        ]
    }
    manifest = parse_manifest(payload)
    assert manifest is not None
    assert "description" not in manifest["tools"][0]


@pytest.mark.mocked
def test_parse_manifest_omits_missing_body():
    """parse_manifest omits body field when not in payload."""
    payload = {
        "tools": [
            {
                "name": "repo-tips",
                "source": "skill",
            }
        ]
    }
    manifest = parse_manifest(payload)
    assert manifest is not None
    assert "body" not in manifest["tools"][0]


@pytest.mark.mocked
def test_parse_manifest_description_and_body_together():
    """parse_manifest preserves both description and body when present."""
    payload = {
        "tools": [
            {
                "name": "repo-tips",
                "source": "skill",
                "description": "Repository tips",
                "body": "Content goes here",
            }
        ]
    }
    manifest = parse_manifest(payload)
    assert manifest is not None
    tool = manifest["tools"][0]
    assert tool["description"] == "Repository tips"
    assert tool["body"] == "Content goes here"


# === Tests for client_doc_skills helper ===


@pytest.mark.mocked
def test_client_doc_skills_none_manifest():
    """client_doc_skills(None) returns empty dict."""
    assert client_doc_skills(None) == {}


@pytest.mark.mocked
def test_client_doc_skills_empty_manifest():
    """client_doc_skills with empty tools list returns empty dict."""
    manifest: ClientManifest = {"tools": []}
    assert client_doc_skills(manifest) == {}


@pytest.mark.mocked
def test_client_doc_skills_extracts_schema_less_skill():
    """client_doc_skills extracts source='skill' without schema."""
    manifest: ClientManifest = {
        "tools": [
            {
                "name": "repo-tips",
                "source": "skill",
                "description": "Repository guidelines",
                "body": "Follow these practices...",
            }
        ]
    }
    doc_skills = client_doc_skills(manifest)
    assert "repo-tips" in doc_skills
    assert doc_skills["repo-tips"]["description"] == "Repository guidelines"
    assert doc_skills["repo-tips"]["body"] == "Follow these practices..."


@pytest.mark.mocked
def test_client_doc_skills_excludes_hosted_skill_with_schema():
    """client_doc_skills excludes source='skill' with valid schema."""
    schema = {"type": "function", "function": {"name": "generate"}}
    manifest: ClientManifest = {
        "tools": [
            {
                "name": "code-gen",
                "source": "skill",
                "namespace": "code-gen",
                "schema": schema,
                "description": "Code generation",
                "body": "Not extracted",
            }
        ]
    }
    doc_skills = client_doc_skills(manifest)
    # Excluded because it has a valid schema (it's a hosted pod skill, not documentation).
    assert "code-gen" not in doc_skills


@pytest.mark.mocked
def test_client_doc_skills_excludes_mcp():
    """client_doc_skills excludes source='mcp' descriptors."""
    manifest: ClientManifest = {
        "tools": [
            {
                "name": "find_references",
                "source": "mcp",
                "description": "Find references",
                "body": "Body",
            }
        ]
    }
    doc_skills = client_doc_skills(manifest)
    assert doc_skills == {}


@pytest.mark.mocked
def test_client_doc_skills_excludes_builtin():
    """client_doc_skills excludes source='builtin' descriptors."""
    manifest: ClientManifest = {
        "tools": [
            {
                "name": "read_file",
                "source": "builtin",
                "description": "Read a file",
                "body": "Body",
            }
        ]
    }
    doc_skills = client_doc_skills(manifest)
    assert doc_skills == {}


@pytest.mark.mocked
def test_client_doc_skills_defaults_empty_strings():
    """client_doc_skills uses empty string defaults for missing description/body."""
    manifest: ClientManifest = {
        "tools": [
            {
                "name": "repo-tips",
                "source": "skill",
            }
        ]
    }
    doc_skills = client_doc_skills(manifest)
    assert doc_skills["repo-tips"]["description"] == ""
    assert doc_skills["repo-tips"]["body"] == ""


@pytest.mark.mocked
def test_client_doc_skills_skips_no_name():
    """client_doc_skills skips descriptors without a name."""
    manifest: ClientManifest = {
        "tools": [
            {
                "source": "skill",
                "description": "No name",
                "body": "Body",
            }
        ]
    }
    doc_skills = client_doc_skills(manifest)
    assert doc_skills == {}


@pytest.mark.mocked
def test_client_doc_skills_multiple():
    """client_doc_skills extracts multiple documentation skills."""
    manifest: ClientManifest = {
        "tools": [
            {
                "name": "repo-tips",
                "source": "skill",
                "description": "Repository tips",
                "body": "Tips...",
            },
            {
                "name": "conventions",
                "source": "skill",
                "description": "Naming conventions",
                "body": "Conventions...",
            },
        ]
    }
    doc_skills = client_doc_skills(manifest)
    assert len(doc_skills) == 2
    assert doc_skills["repo-tips"]["description"] == "Repository tips"
    assert doc_skills["conventions"]["description"] == "Naming conventions"


# === Tests for build_tool_list with client doc-skills ===


@pytest.mark.mocked
def test_build_tool_list_doc_skill_added_to_load_skill_enum():
    """build_tool_list adds doc-skills to load_skill enum."""
    manifest: ClientManifest = {
        "tools": [
            {"name": "read_file", "source": "builtin"},
            {
                "name": "repo-tips",
                "source": "skill",
                "description": "Repository tips",
                "body": "Content",
            },
        ]
    }
    static_tail = _static_tail_schemas()
    tools = build_tool_list(
        manifest, skill_router=None, codegraph_enabled=False, static_tail=static_tail
    )
    # load_skill should be present with enum including "repo-tips".
    load_skill = next((t for t in tools if t["function"]["name"] == "load_skill"), None)
    assert load_skill is not None
    enum = load_skill["function"]["parameters"]["properties"]["name"]["enum"]
    assert "repo-tips" in enum


@pytest.mark.mocked
def test_build_tool_list_doc_skill_merged_with_pod_enum():
    """build_tool_list merges doc-skills with pod-skills in enum."""
    runner = MagicMock()

    def mock_tool_schema(exclude=frozenset()):
        enum_skills = [s for s in ["code-review", "test-gen"] if s not in exclude]
        return {
            "type": "function",
            "function": {
                "name": "load_skill",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "enum": enum_skills,
                        }
                    },
                    "required": ["name"],
                },
            },
        }

    runner.tool_schema = mock_tool_schema
    sr = MagicMock()
    sr.runner = runner

    manifest: ClientManifest = {
        "tools": [
            {"name": "read_file", "source": "builtin"},
            {
                "name": "repo-tips",
                "source": "skill",
                "description": "Repository tips",
                "body": "Content",
            },
        ]
    }
    static_tail = _static_tail_schemas()
    tools = build_tool_list(
        manifest, skill_router=sr, codegraph_enabled=False, static_tail=static_tail
    )
    load_skill = next((t for t in tools if t["function"]["name"] == "load_skill"), None)
    assert load_skill is not None
    enum = load_skill["function"]["parameters"]["properties"]["name"]["enum"]
    # Should have sorted union: ["code-review", "repo-tips", "test-gen"]
    assert set(enum) == {"code-review", "repo-tips", "test-gen"}
    assert enum == sorted(enum)  # Verify sorted


@pytest.mark.mocked
def test_build_tool_list_doc_skills_only_no_router():
    """build_tool_list advertises load_skill with doc-skills only when no router."""
    manifest: ClientManifest = {
        "tools": [
            {"name": "read_file", "source": "builtin"},
            {
                "name": "repo-tips",
                "source": "skill",
                "description": "Tips",
                "body": "Content",
            },
        ]
    }
    static_tail = _static_tail_schemas()
    tools = build_tool_list(
        manifest, skill_router=None, codegraph_enabled=False, static_tail=static_tail
    )
    names = [t["function"]["name"] for t in tools]
    assert "load_skill" in names
    assert "call_skill_tool" in names
    load_skill = next((t for t in tools if t["function"]["name"] == "load_skill"), None)
    enum = load_skill["function"]["parameters"]["properties"]["name"]["enum"]
    assert enum == ["repo-tips"]


@pytest.mark.mocked
def test_build_tool_list_doc_skill_body_not_in_tool_list():
    """build_tool_list does not include doc-skill body in the tool list."""
    manifest: ClientManifest = {
        "tools": [
            {
                "name": "repo-tips",
                "source": "skill",
                "description": "Tips",
                "body": "THIS_BODY_CONTENT_MUST_NOT_APPEAR",
            },
        ]
    }
    static_tail = _static_tail_schemas()
    tools = build_tool_list(
        manifest, skill_router=None, codegraph_enabled=False, static_tail=static_tail
    )
    # Serialize and check that the body text is NOT present.
    import json

    tools_json = json.dumps(tools)
    assert "THIS_BODY_CONTENT_MUST_NOT_APPEAR" not in tools_json


# === Tests for PromptAssembler catalog text with doc-skills ===


@pytest.mark.mocked
def test_prompt_assembler_catalog_includes_doc_skill():
    """PromptAssembler system message includes doc-skills in catalog text."""
    manifest: ClientManifest = {
        "tools": [
            {"name": "read_file", "source": "builtin"},
            {
                "name": "repo-tips",
                "source": "skill",
                "description": "Repository tips and conventions",
                "body": "THIS_BODY_MUST_NOT_BE_IN_SYSTEM",
            },
        ]
    }
    pa = PromptAssembler(
        skill_router=None,
        codegraph_enabled=False,
        client_manifest=manifest,
    )
    system_text = pa.system_message()["content"]
    # Description should be in the system message.
    assert "repo-tips" in system_text
    assert "Repository tips and conventions" in system_text
    # Body should NOT be in the system message.
    assert "THIS_BODY_MUST_NOT_BE_IN_SYSTEM" not in system_text


@pytest.mark.mocked
def test_prompt_assembler_no_doc_skills_catalog_unchanged():
    """PromptAssembler doesn't add doc-skills lines when manifest has no doc-skills."""
    manifest_with_doc_skills: ClientManifest = {
        "tools": [
            {"name": "read_file", "source": "builtin"},
            {
                "name": "repo-tips",
                "source": "skill",
                "description": "Repository tips",
                "body": "Content",
            },
        ]
    }
    manifest_no_doc_skills: ClientManifest = {
        "tools": [
            {"name": "read_file", "source": "builtin"},
        ]
    }
    # Build with doc-skills.
    pa_with_doc = PromptAssembler(
        skill_router=None,
        codegraph_enabled=False,
        client_manifest=manifest_with_doc_skills,
    )
    # Build without doc-skills.
    pa_without_doc = PromptAssembler(
        skill_router=None,
        codegraph_enabled=False,
        client_manifest=manifest_no_doc_skills,
    )
    # The one with doc-skills should have "- repo-tips:" in the system message.
    system_with = pa_with_doc.system_message()["content"]
    system_without = pa_without_doc.system_message()["content"]
    assert "- repo-tips:" in system_with
    assert "- repo-tips:" not in system_without


@pytest.mark.mocked
def test_prompt_assembler_multiple_doc_skills_sorted():
    """PromptAssembler sorts doc-skills alphabetically in system message."""
    manifest: ClientManifest = {
        "tools": [
            {
                "name": "zebra-tips",
                "source": "skill",
                "description": "Z tips",
                "body": "Z body",
            },
            {
                "name": "alpha-tips",
                "source": "skill",
                "description": "A tips",
                "body": "A body",
            },
        ]
    }
    pa = PromptAssembler(
        skill_router=None,
        codegraph_enabled=False,
        client_manifest=manifest,
    )
    system_text = pa.system_message()["content"]
    # Find the indices of the two skills.
    idx_alpha = system_text.find("- alpha-tips:")
    idx_zebra = system_text.find("- zebra-tips:")
    # alpha should come before zebra.
    assert idx_alpha < idx_zebra and idx_alpha > 0 and idx_zebra > 0


@pytest.mark.mocked
def test_prompt_assembler_doc_skill_body_never_in_prefix():
    """PromptAssembler canonical_prefix never includes doc-skill body."""
    manifest: ClientManifest = {
        "tools": [
            {
                "name": "repo-tips",
                "source": "skill",
                "description": "Tips",
                "body": "SECRET_BODY_CONTENT_HERE",
            },
        ]
    }
    pa = PromptAssembler(
        skill_router=None,
        codegraph_enabled=False,
        client_manifest=manifest,
    )
    prefix = pa.canonical_prefix()
    assert "SECRET_BODY_CONTENT_HERE" not in prefix


# === Byte-stability regression ===


@pytest.mark.mocked
def test_prompt_assembler_no_manifest_byte_identical():
    """PromptAssembler with manifest=None is byte-identical to before."""
    pa_no_client = PromptAssembler(skill_router=None, codegraph_enabled=False, client_manifest=None)
    pa_explicit_none = PromptAssembler(
        skill_router=None, codegraph_enabled=False, client_manifest=None
    )
    assert pa_no_client.canonical_prefix() == pa_explicit_none.canonical_prefix()


@pytest.mark.mocked
def test_build_tool_list_manifest_without_doc_skills_identical():
    """build_tool_list with no doc-skills matches pre-change behavior."""
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
    # Should NOT have load_skill or call_skill_tool (no router, no doc-skills).
    assert "load_skill" not in names
    assert "call_skill_tool" not in names
    # Should have the declared builtins + finish.
    assert names == ["read_file", "write_file", "finish"]
