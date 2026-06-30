"""
Cross-language contract integration test for P2-B.1 documentation skills.

The frontend (TS) `skillDescriptors()` emits, and `capabilitiesFrame()` ships, a
manifest tool descriptor of the EXACT shape:

    { "name": str, "source": "skill", "description": str, "body": str }

This test feeds that exact frontend-produced frame payload through the backend
(Python) consumer pipeline — parse_manifest -> client_doc_skills -> build_tool_list
-> PromptAssembler catalog -> load_skill resolution — to prove the two sides agree
on field names and the body-isolation / catalog-merge guarantees hold end to end.

The producer and consumer were built in parallel against the locked contract, so
this is the seam most at risk of silent drift; a green here means a discovered
SKILL.md really round-trips to a load_skill body at runtime.
"""

from __future__ import annotations

import json

from services.orchestrator.prompt_assembler import PromptAssembler, _static_tail_schemas
from services.orchestrator.tool_manifest import (
    build_tool_list,
    client_doc_skills,
    parse_manifest,
)

# A byte-for-byte mirror of what the frontend `capabilitiesFrame([...skillDescriptors])`
# emits: the five builtins (CLIENT_CAPABILITIES.tools) + one discovered doc-skill.
SKILL_NAME = "repo-tips"
SKILL_DESCRIPTION = "Conventions and shortcuts for navigating this repository"
SKILL_BODY = (
    "# Repo Tips\n\n"
    "ALWAYS_SEARCH_FIRST: use search_files before reading.\n"
    "UNIQUE_BODY_SENTINEL_4f9c: this string must never reach the model prefix."
)

FRONTEND_FRAME: dict = {
    "type": "client.capabilities",
    "protocolVersion": 1,
    "tools": [
        {"name": "read_file", "source": "builtin"},
        {"name": "write_file", "source": "builtin"},
        {"name": "list_dir", "source": "builtin"},
        {"name": "search_files", "source": "builtin"},
        {"name": "run_tests", "source": "builtin"},
        {
            "name": SKILL_NAME,
            "source": "skill",
            "description": SKILL_DESCRIPTION,
            "body": SKILL_BODY,
        },
    ],
}


def _manifest():
    manifest = parse_manifest(FRONTEND_FRAME)
    assert manifest is not None, "frontend frame must parse into a manifest"
    return manifest


def test_parse_manifest_preserves_doc_skill_fields():
    """parse_manifest must carry description + body off the frontend descriptor."""
    manifest = _manifest()
    skill = next(t for t in manifest["tools"] if t["name"] == SKILL_NAME)
    assert skill["source"] == "skill"
    assert skill["description"] == SKILL_DESCRIPTION
    assert skill["body"] == SKILL_BODY
    # Schema-less by contract (so it is NOT advertised as a flat MCP tool).
    assert "schema" not in skill
    assert "namespace" not in skill


def test_client_doc_skills_extracts_frontend_shape():
    """client_doc_skills resolves the frontend doc-skill by name -> description+body."""
    doc = client_doc_skills(_manifest())
    assert set(doc.keys()) == {SKILL_NAME}
    assert doc[SKILL_NAME]["description"] == SKILL_DESCRIPTION
    assert doc[SKILL_NAME]["body"] == SKILL_BODY


def test_build_tool_list_advertises_doc_skill_without_leaking_body():
    """load_skill enum includes the doc-skill name; the body never enters the tool list."""
    tools = build_tool_list(
        _manifest(),
        skill_router=None,
        codegraph_enabled=False,
        memory_enabled=False,
        static_tail=_static_tail_schemas(),
    )
    load_skill = next((t for t in tools if t.get("function", {}).get("name") == "load_skill"), None)
    assert load_skill is not None, "load_skill must be advertised for a doc-skill-only client"
    enum = load_skill["function"]["parameters"]["properties"]["name"]["enum"]
    assert SKILL_NAME in enum
    # The body must not appear anywhere in the serialized tool list (prefix safety).
    assert SKILL_BODY not in json.dumps(tools)
    assert "UNIQUE_BODY_SENTINEL_4f9c" not in json.dumps(tools)


def test_prompt_assembler_catalog_has_description_not_body():
    """The system prompt advertises the doc-skill by description, never its body."""
    assembler = PromptAssembler(
        skill_router=None,
        codegraph_enabled=False,
        memory_enabled=False,
        client_manifest=_manifest(),
    )
    system_text = assembler.system_message()["content"]
    assert f"- {SKILL_NAME}: {SKILL_DESCRIPTION}" in system_text
    assert "UNIQUE_BODY_SENTINEL_4f9c" not in system_text
    assert SKILL_BODY not in assembler.canonical_prefix()


def test_load_skill_resolution_returns_the_body():
    """The dispatch path (client_doc_skills lookup) returns the exact frontend body."""
    doc = client_doc_skills(_manifest()).get(SKILL_NAME)
    assert doc is not None
    # Mirror the coding_orchestrator load_skill dispatch result shape.
    obs = {
        "name": "load_skill",
        "response": {"status": "loaded", "name": SKILL_NAME, "body": doc["body"]},
    }
    assert obs["response"]["body"] == SKILL_BODY
