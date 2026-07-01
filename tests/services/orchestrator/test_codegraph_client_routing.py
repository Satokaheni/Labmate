"""
P2-C / P2-D — client-side CodeGraph via the MCP-host path (Framing A).

CodeGraph v0.9.9 is itself an MCP server. A client hosts it (P2-B.3,
`~/.labmate/mcp.json`) and declares its tools as `mcp__codegraph__*`. The model
then uses CodeGraph's full toolset (search/explore/callers/…) routed to the
client over the existing tool.request/tool.result seam — no new backend routing.

These tests lock the two guarantees that make that work end to end:
  - P2-C: when a client hosts CodeGraph, the POD `code_semantic_search` is NOT
    advertised (build_tool_list only advertises it when the manifest declares it),
    and the `mcp__codegraph__*` tools ARE advertised AND route to the client.
  - P2-D: the pod embedder spawn is gated by `ENABLE_POD_CODEGRAPH` so a
    client-first deployment can skip it.
"""

from __future__ import annotations

import pytest

from services.orchestrator.main import pod_codegraph_enabled
from services.orchestrator.prompt_assembler import _static_tail_schemas
from services.orchestrator.tool_manifest import (
    build_tool_list,
    manifest_local_tool_names,
    parse_manifest,
)


def _codegraph_frame() -> dict:
    """A frontend frame: builtins + a hosted CodeGraph MCP server (Framing A)."""
    return {
        "protocolVersion": 1,
        "tools": [
            {"name": "read_file", "source": "builtin"},
            {"name": "search_files", "source": "builtin"},
            {
                "name": "codegraph_search",
                "source": "mcp",
                "namespace": "codegraph",
                "schema": {
                    "type": "function",
                    "function": {
                        "name": "codegraph_search",
                        "description": "semantic code search",
                        "parameters": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                    },
                },
            },
            {
                "name": "codegraph_explore",
                "source": "mcp",
                "namespace": "codegraph",
                "schema": {
                    "type": "function",
                    "function": {
                        "name": "codegraph_explore",
                        "description": "explore the graph",
                        "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
                    },
                },
            },
        ],
    }


def test_codegraph_hosting_client_excludes_pod_semantic_search():
    """A client hosting CodeGraph as MCP must NOT also get the pod code_semantic_search
    (no duplicate semantic-search tool) — even when the pod embedder is enabled."""
    manifest = parse_manifest(_codegraph_frame())
    tools = build_tool_list(
        manifest, skill_router=None, codegraph_enabled=True, static_tail=_static_tail_schemas()
    )
    names = [t["function"]["name"] for t in tools]
    assert "code_semantic_search" not in names  # pod tool excluded (correct by construction)
    assert "mcp__codegraph__codegraph_search" in names
    assert "mcp__codegraph__codegraph_explore" in names


def test_codegraph_mcp_tools_route_to_the_client():
    """The hosted `mcp__codegraph__*` tools must route to the CLIENT (be in the
    local-tool set), so the model's calls go over the tool.request/result seam."""
    manifest = parse_manifest(_codegraph_frame())
    local = manifest_local_tool_names(manifest, fallback={"read_file"})
    assert "mcp__codegraph__codegraph_search" in local
    assert "mcp__codegraph__codegraph_explore" in local


def test_client_without_codegraph_gets_no_pod_semantic_search():
    """Intended P2-D behavior: a client that does NOT host CodeGraph gets no pod
    semantic search either (the pod tool is fallback/no-client only)."""
    frame = {"protocolVersion": 1, "tools": [{"name": "read_file", "source": "builtin"}]}
    manifest = parse_manifest(frame)
    tools = build_tool_list(
        manifest, skill_router=None, codegraph_enabled=True, static_tail=_static_tail_schemas()
    )
    assert "code_semantic_search" not in [t["function"]["name"] for t in tools]


def test_no_client_fallback_still_advertises_pod_semantic_search():
    """No client attached (manifest None) + pod embedder enabled → the pod
    code_semantic_search IS advertised (the fallback path is unchanged)."""
    tools = build_tool_list(
        None, skill_router=None, codegraph_enabled=True, static_tail=_static_tail_schemas()
    )
    assert "code_semantic_search" in [t["function"]["name"] for t in tools]


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, True),  # unset → default on (unchanged behavior)
        ("1", True),
        ("0", False),
        ("false", False),
        ("FALSE", False),
        ("no", False),
        ("true", True),
    ],
)
def test_pod_codegraph_enabled_flag(monkeypatch, value, expected):
    """P2-D: ENABLE_POD_CODEGRAPH gates the pod embedder spawn."""
    if value is None:
        monkeypatch.delenv("ENABLE_POD_CODEGRAPH", raising=False)
    else:
        monkeypatch.setenv("ENABLE_POD_CODEGRAPH", value)
    assert pod_codegraph_enabled() is expected
