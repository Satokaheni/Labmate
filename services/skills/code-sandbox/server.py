"""MCP server entry point for the code-sandbox skill.

Exposes run_python, run_shell, run_tests, install_packages over stdio.
CRITICAL: stdout carries JSON-RPC. All logging goes to stderr.
"""
import sys
import json
import logging

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from executor import DockerExecutor

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("code-sandbox.server")

app = Server("code-sandbox")
_executor: DockerExecutor | None = None


def get_executor() -> DockerExecutor:
    global _executor
    if _executor is None:
        _executor = DockerExecutor()
    return _executor


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="code_sandbox.run_python",
            description="Execute Python code in an isolated container. Returns JSON "
            "with stdout, stderr, exit_code, duration_ms, timed_out.",
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "timeout": {"type": "integer", "default": 30},
                    "packages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                    },
                },
                "required": ["code"],
            },
        ),
        Tool(
            name="code_sandbox.run_shell",
            description="Execute a shell command in an isolated container. Returns JSON "
            "with stdout, stderr, exit_code, duration_ms, timed_out.",
            inputSchema={
                "type": "object",
                "properties": {
                    "cmd": {"type": "string"},
                    "timeout": {"type": "integer", "default": 30},
                },
                "required": ["cmd"],
            },
        ),
        Tool(
            name="code_sandbox.run_tests",
            description="Run a test suite in an isolated container. Returns JSON with "
            "passed, failed, errors, duration_ms, output, timed_out.",
            inputSchema={
                "type": "object",
                "properties": {
                    "test_path": {"type": "string"},
                    "framework": {"type": "string", "default": "pytest"},
                    "timeout": {"type": "integer", "default": 120},
                },
                "required": ["test_path"],
            },
        ),
        Tool(
            name="code_sandbox.install_packages",
            description="Install Python packages into a throwaway sandbox to verify "
            "they resolve. Returns JSON execution result.",
            inputSchema={
                "type": "object",
                "properties": {
                    "packages": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["packages"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    executor = get_executor()
    try:
        if name == "code_sandbox.run_python":
            result = executor.run_python(
                arguments["code"],
                timeout=arguments.get("timeout", 30),
                packages=arguments.get("packages", []),
            )
        elif name == "code_sandbox.run_shell":
            result = executor.run_shell(
                arguments["cmd"], timeout=arguments.get("timeout", 30)
            )
        elif name == "code_sandbox.run_tests":
            result = executor.run_tests(
                arguments["test_path"],
                framework=arguments.get("framework", "pytest"),
                timeout=arguments.get("timeout", 120),
            )
        elif name == "code_sandbox.install_packages":
            result = executor.run_python(
                "print('packages installed')",
                packages=arguments["packages"],
            )
        else:
            raise ValueError(f"unknown tool: {name}")
        return [TextContent(type="text", text=result.model_dump_json())]
    except Exception as e:
        logger.exception("tool %s failed", name)
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def main() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
