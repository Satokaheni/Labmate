import json
import os
import pytest

from tests.live.conftest import require_service

pytestmark = pytest.mark.live

BRIDGE = os.path.join(
    os.path.dirname(__file__), "..", "..", "services", "mcp-bridge", "dist", "index.js"
)


async def _call_exec_run(command: str, timeout: int):
    """Spawn the bridge over stdio and call exec_run once. Returns (text, isError)."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.shared.exceptions import McpError

    params = StdioServerParameters(command="node", args=[BRIDGE], env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            try:
                res = await session.call_tool(
                    "exec_run", {"command": command, "cwd": os.getcwd(), "timeout": timeout}
                )
                text = "\n".join(c.text for c in res.content if hasattr(c, "text"))
                return text, bool(res.isError)
            except McpError as e:
                # Schema validation errors come through as McpError
                return str(e), True


@pytest.mark.asyncio
async def test_plain_command_runs():
    require_service(lambda: os.path.exists(BRIDGE), "mcp-bridge dist")
    text, is_error = await _call_exec_run("echo hello-live", 10000)
    assert not is_error
    assert "hello-live" in text


@pytest.mark.asyncio
async def test_pytest_is_blocked_through_exec_run():
    require_service(lambda: os.path.exists(BRIDGE), "mcp-bridge dist")
    text, is_error = await _call_exec_run("pytest -q", 10000)
    assert is_error
    assert "not allowed" in text.lower() or "code-sandbox" in text.lower()


@pytest.mark.asyncio
async def test_timeout_above_cap_is_rejected():
    require_service(lambda: os.path.exists(BRIDGE), "mcp-bridge dist")
    # exec_run schema caps timeout at 60000ms; 120000 must be rejected, not run.
    text, is_error = await _call_exec_run("echo x", 120000)
    assert is_error
