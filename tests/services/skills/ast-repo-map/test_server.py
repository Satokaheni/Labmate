"""Schema/handler contract tests for the ast-repo-map MCP server.

Regression guard for the discovery-call bug: get_repo_map used to mark
`chat_files` required, so a discovery call (no files being edited) was rejected
by schema validation before it ever reached the mapper — even though the mapper
treats chat_files=[] as a normal whole-repo map.
"""
import pytest
from mcp.types import TextContent


@pytest.mark.asyncio
async def test_get_repo_map_does_not_require_chat_files(repo_mapper):
    import server

    tools = await server.list_tools()
    get_map = next(t for t in tools if t.name == "get_repo_map")
    required = get_map.inputSchema.get("required", [])
    # Discovery callers have no chat_files yet; requiring it breaks that use.
    assert "chat_files" not in required


@pytest.mark.asyncio
async def test_get_repo_map_runs_with_no_arguments(repo_mapper, sample_repo, monkeypatch):
    import server

    monkeypatch.setenv("REPO_ROOT", str(sample_repo))
    server.mapper = None  # force re-init against the fixture repo

    # No chat_files and no max_tokens — the discovery case that used to error.
    result = await server.call_tool("get_repo_map", {})

    assert isinstance(result, list) and result
    assert isinstance(result[0], TextContent)
    assert not result[0].text.startswith("error:"), result[0].text
