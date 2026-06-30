"""
Client tool-manifest contract: declarations of which tools a client can execute.

The manifest is sent by the client on connect and threaded through the orchestrator
to build a capability-aware tool list. When no manifest is present, the fallback
is the full tool list (read/write/list/run_bash/run_tests/finish).
"""

from __future__ import annotations

from typing import Any, TypedDict


class ToolDescriptor(TypedDict, total=False):
    """OpenAI-compatible tool descriptor."""

    name: str  # required: tool name
    source: str  # "builtin" | "mcp" | "skill"; default "builtin"
    namespace: str | None  # optional: mcp/skill namespace
    schema: dict | None  # optional: full OpenAI tool object
    description: str  # optional: documentation skill description
    body: str  # optional: documentation skill body


class ClientManifest(TypedDict, total=False):
    """Top-level manifest of client capabilities."""

    protocol_version: int  # optional; if present, usually 1
    tools: list[ToolDescriptor]  # required: list of tool descriptors


# Canonical builtin schemas — byte-identical to prompt_assembler._static_tail_schemas()
# so prefixes never drift. Managed here; imported into prompt_assembler.
CANONICAL_BUILTIN_SCHEMAS: dict[str, dict] = {
    "read_file": {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file from the user's local workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative file path"}
                },
                "required": ["path"],
            },
        },
    },
    "write_file": {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write (create or overwrite) a UTF-8 text file in the user's local workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative file path"},
                    "content": {"type": "string", "description": "Full file contents to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    "list_dir": {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List entries of a directory in the user's local workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative directory path"}
                },
                "required": ["path"],
            },
        },
    },
    "search_files": {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search the user's local workspace for a regex pattern (ripgrep). Use this FIRST to LOCATE code before reading it. Returns matching {file, line, text} hits.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Regular expression to search for"},
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative subdir to scope the search, optional",
                    },
                    "glob": {
                        "type": "string",
                        "description": "Only search files matching this glob, optional",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Cap on number of hits, optional",
                    },
                },
                "required": ["query"],
            },
        },
    },
    "run_tests": {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": (
                "Run the project's REAL test suite (pytest) and return the RAW "
                "pass/fail output. Use this to VERIFY a fix actually works — do not "
                "claim tests pass without calling this and reading its raw_output. "
                "Optional 'path' scopes to a file/dir/nodeid; optional 'expr' is a "
                "pytest -k expression. Returns {ok, exit_code, raw_output}."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File, dir, or pytest nodeid to test. Omit to run the whole suite.",
                    },
                    "expr": {
                        "type": "string",
                        "description": "pytest -k expression to select tests, optional.",
                    },
                    "timeout_ms": {
                        "type": "integer",
                        "description": "Max run time in milliseconds, optional.",
                    },
                },
                "required": [],
            },
        },
    },
}


def _is_usable_descriptor(descriptor: ToolDescriptor) -> bool:
    """
    Check if a descriptor is usable (can be advertised and dispatched).

    An mcp/skill descriptor is usable ONLY if it carries a valid schema:
    a dict with a "function" key (the OpenAI tool object).
    Builtin descriptors are ALWAYS usable (backend supplies schema; descriptor
    has no schema by design).

    Returns:
        True if the descriptor should be advertised and included in dispatch routing.
    """
    source = descriptor.get("source", "builtin")
    # Builtin descriptors are always usable (schema supplied by backend).
    if source == "builtin":
        return True
    # mcp/skill descriptors must have a valid schema.
    schema = descriptor.get("schema")
    return isinstance(schema, dict) and isinstance(schema.get("function"), dict)


def _final_tool_name(descriptor: ToolDescriptor) -> str:
    """
    Compute the final dispatch name for a tool descriptor.

    Implements the EXACT namespacing rule: if descriptor has both namespace
    and source in {"mcp", "skill"}, the final name is f"mcp__{namespace}__{name}";
    otherwise just the tool name.

    This helper is used consistently in both build_tool_list and
    manifest_local_tool_names to prevent drift.
    """
    namespace = descriptor.get("namespace")
    source = descriptor.get("source", "builtin")
    name = descriptor.get("name", "")

    if namespace and source in ("mcp", "skill"):
        return f"mcp__{namespace}__{name}"
    return name


def manifest_local_tool_names(manifest: ClientManifest | None, fallback: set[str]) -> set[str]:
    """
    Extract the set of tool names that route to the local client.

    If manifest is None (no client attached), returns the fallback set.
    Otherwise, returns the set of final dispatch names for every usable tool
    declared in the manifest, using _final_tool_name to compute each name.

    A tool is usable if it is a builtin OR carries a valid schema (dict with "function" key).
    Schema-less mcp/skill descriptors are excluded.

    This set is used in dispatch routing to check `if name in local_tool_names`.
    """
    if manifest is None:
        return set(fallback)

    names: set[str] = set()
    for descriptor in manifest.get("tools", []):
        if "name" in descriptor and _is_usable_descriptor(descriptor):
            names.add(_final_tool_name(descriptor))

    return names


def parse_manifest(payload: dict | None) -> ClientManifest | None:
    """
    Parse a client manifest from a raw dict payload.

    Returns None if payload is falsy, not a dict, or has no non-empty tools list
    (the "no client attached" signal).

    Tolerant: accepts both camelCase (protocolVersion) and snake_case (protocol_version).
    Ignores unknown fields. Skips malformed tool entries. Defaults source to "builtin".
    """
    if not payload or not isinstance(payload, dict):
        return None

    # Normalize keys: accept both camelCase and snake_case.
    protocol_version = payload.get("protocolVersion") or payload.get("protocol_version")
    tools_raw = payload.get("tools") or []

    if not tools_raw:
        return None

    # Filter malformed tools (each must have at least a name).
    tools: list[ToolDescriptor] = []
    for tool_raw in tools_raw:
        if not isinstance(tool_raw, dict) or "name" not in tool_raw:
            continue
        # Build a descriptor with defaults.
        descriptor: ToolDescriptor = {
            "name": tool_raw["name"],
            "source": tool_raw.get("source", "builtin"),
        }
        if "namespace" in tool_raw and tool_raw["namespace"] is not None:
            descriptor["namespace"] = tool_raw["namespace"]
        if "schema" in tool_raw and tool_raw["schema"] is not None:
            descriptor["schema"] = tool_raw["schema"]
        if "description" in tool_raw and tool_raw["description"] is not None:
            descriptor["description"] = tool_raw["description"]
        if "body" in tool_raw and tool_raw["body"] is not None:
            descriptor["body"] = tool_raw["body"]
        tools.append(descriptor)

    if not tools:
        return None

    manifest: ClientManifest = {"tools": tools}
    if protocol_version is not None:
        manifest["protocol_version"] = protocol_version

    return manifest


def client_doc_skills(manifest: ClientManifest | None) -> dict[str, dict]:
    """
    Extract documentation skills from a client manifest.

    A documentation skill is a descriptor with:
    - source == "skill" (documentation skills are never "mcp" or "builtin")
    - NO valid schema (i.e. missing schema or schema without "function" key)
    - A name
    - Optional description and body

    Returns a dict mapping skill name to {description: str, body: str}.
    Returns {} if manifest is None or has no documentation skills.
    """
    if manifest is None:
        return {}

    doc_skills: dict[str, dict] = {}
    for descriptor in manifest.get("tools", []):
        # Only consider source="skill" descriptors.
        source = descriptor.get("source", "builtin")
        if source != "skill":
            continue

        # Skip if it has a valid schema (that means it's a hosted pod skill, not documentation).
        if _is_usable_descriptor(descriptor):
            continue

        name = descriptor.get("name")
        if not name:
            continue

        # Extract description and body (with defaults).
        description = descriptor.get("description", "")
        body = descriptor.get("body", "")

        doc_skills[name] = {"description": description, "body": body}

    return doc_skills


def hosted_skill_namespaces(manifest: ClientManifest | None) -> set[str]:
    """
    Extract the skill namespaces that a client hosts directly via mcp/skill descriptors.

    Used to drop duplicate pod-catalog entries so the model doesn't see a skill twice
    (once as client-hosted mcp/skill tool, once as pod-side load_skill).

    Args:
        manifest: Parsed ClientManifest or None.

    Returns:
        Set of skill namespace strings (e.g., {'ast-ts-refactor', 'code-review'}).
        Empty set if manifest is None.
    """
    if manifest is None:
        return set()
    out: set[str] = set()
    for descriptor in manifest.get("tools", []):
        source = descriptor.get("source", "builtin")
        namespace = descriptor.get("namespace")
        if source in ("mcp", "skill") and namespace and _is_usable_descriptor(descriptor):
            out.add(namespace)
    return out


def _call_skill_tool_schema() -> dict:
    """Import from prompt_assembler to avoid circular dependency."""
    from services.orchestrator.prompt_assembler import _call_skill_tool_schema as _schema

    return _schema()


def build_tool_list(
    manifest: ClientManifest | None,
    *,
    skill_router: Any = None,
    codegraph_enabled: bool = False,
    memory_enabled: bool = False,
    static_tail: list[dict],
) -> list[dict]:
    """
    Build the tool list for a goal, respecting client capabilities or falling back to legacy behavior.

    Args:
        manifest: Parsed ClientManifest or None (no client attached, use full fallback).
        skill_router: SkillRouter instance, if skills are enabled.
        codegraph_enabled: Whether codegraph MCP is active.
        memory_enabled: Whether memory store is active.
        static_tail: Result of prompt_assembler._static_tail_schemas(); used for fallback.

    Returns:
        Ordered tool list, byte-stable, deterministic.
    """
    if manifest is None:
        # Fallback: legacy no-client behavior — full tool list unchanged.
        tools: list[dict] = []
        if skill_router is not None:
            tools.append(skill_router.runner.tool_schema())  # load_skill
            tools.append(_call_skill_tool_schema())
        if codegraph_enabled:
            from services.orchestrator.prompt_assembler import _code_semantic_search_schema

            tools.append(_code_semantic_search_schema())
        if memory_enabled:
            from services.orchestrator.prompt_assembler import _memory_search_schema

            tools.append(_memory_search_schema())
        tools.extend(static_tail)
        return tools

    # Client attached: capability-aware tool list.
    tools = []
    manifest_tool_names = {t["name"] for t in manifest["tools"]}

    # Compute the set of skill namespaces the client hosts directly (mcp/skill).
    # These should be dropped from the pod-side load_skill enum to prevent duplication.
    hosted_skills = hosted_skill_namespaces(manifest)

    # 1. Skill interface (if router present or client has doc-skills).
    doc_skills = client_doc_skills(manifest)
    if skill_router is not None or doc_skills:
        if skill_router is not None:
            # Exclude hosted skills from the pod catalog.
            load_skill_schema = skill_router.runner.tool_schema(exclude=hosted_skills)
            enum_skills = (
                load_skill_schema.get("function", {})
                .get("parameters", {})
                .get("properties", {})
                .get("name", {})
                .get("enum", [])
            )
        else:
            # No skill_router; use empty enum from pod.
            enum_skills = []

        # Merge client doc-skill names into the enum.
        enum_skills_merged = sorted(set(enum_skills) | set(doc_skills.keys()))

        # Only advertise load_skill if there are available skills after exclusion + merge.
        if enum_skills_merged:
            # Build or update the load_skill schema with the merged enum.
            if skill_router is not None:
                load_skill_schema = skill_router.runner.tool_schema(exclude=hosted_skills)
            else:
                # No pod router; build a minimal schema with just the doc-skills enum.
                load_skill_schema = {
                    "type": "function",
                    "function": {
                        "name": "load_skill",
                        "description": "Load a skill (documentation or pod-based).",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "enum": enum_skills_merged,
                                }
                            },
                            "required": ["name"],
                        },
                    },
                }

            # Update the enum in the schema.
            load_skill_schema = load_skill_schema.copy()
            if "function" in load_skill_schema:
                load_skill_schema["function"] = load_skill_schema["function"].copy()
                if "parameters" in load_skill_schema["function"]:
                    params = load_skill_schema["function"]["parameters"].copy()
                    if "properties" in params:
                        props = params["properties"].copy()
                        if "name" in props:
                            name_prop = props["name"].copy()
                            name_prop["enum"] = enum_skills_merged
                            props["name"] = name_prop
                        params["properties"] = props
                    load_skill_schema["function"]["parameters"] = params

            tools.append(load_skill_schema)
            tools.append(_call_skill_tool_schema())

    # 2. code_semantic_search (only if declared in manifest).
    if "code_semantic_search" in manifest_tool_names:
        from services.orchestrator.prompt_assembler import _code_semantic_search_schema

        tools.append(_code_semantic_search_schema())

    # 3. memory_search (if memory enabled).
    if memory_enabled:
        from services.orchestrator.prompt_assembler import _memory_search_schema

        tools.append(_memory_search_schema())

    # 4. Builtins in fixed order: only those declared in manifest, NO run_bash.
    builtin_order = ["read_file", "write_file", "list_dir", "search_files", "run_tests"]
    for name in builtin_order:
        if name in manifest_tool_names:
            tools.append(CANONICAL_BUILTIN_SCHEMAS[name])

    # 5. Client mcp/skill tools (with valid schemas): sorted by final name for determinism.
    # A schema is valid if it's a dict with a "function" key (OpenAI tool object).
    client_tools: list[tuple[str, dict]] = []
    for descriptor in manifest["tools"]:
        source = descriptor.get("source", "builtin")
        if source not in ("mcp", "skill"):
            continue
        # Skip mcp/skill descriptors without a valid schema.
        if not _is_usable_descriptor(descriptor):
            continue
        # Compute final name using the shared helper.
        final_name = _final_tool_name(descriptor)
        # Clone the schema and update the function name to match final_name.
        schema = descriptor["schema"].copy()
        if "function" in schema:
            schema["function"] = schema["function"].copy()
            schema["function"]["name"] = final_name
        # Store (final_name, schema) for sorting.
        client_tools.append((final_name, schema))

    # Sort by final name for determinism.
    client_tools.sort(key=lambda x: x[0])
    for _, schema in client_tools:
        tools.append(schema)

    # 6. finish (always last).
    finish_schema = next((t for t in static_tail if t["function"]["name"] == "finish"), None)
    if finish_schema:
        tools.append(finish_schema)

    return tools
