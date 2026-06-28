"""MCP server entry point for the code-sandbox skill.

Exposes run_python, run_shell, run_tests, install_packages over stdio.
CRITICAL: stdout carries JSON-RPC. All logging goes to stderr.
"""
import sys
import json
import logging
import os

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from executor import DockerExecutor, LocalSubprocessExecutor

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("code-sandbox.server")

app = Server("code-sandbox")
_executor: DockerExecutor | LocalSubprocessExecutor | None = None


def get_executor() -> DockerExecutor | LocalSubprocessExecutor:
    """Get or create the code executor, with auto-fallback from Docker to local.

    Respects CODE_SANDBOX_BACKEND env var:
      - "docker": force DockerExecutor (raises if daemon unavailable)
      - "local": force LocalSubprocessExecutor (unsandboxed)
      - unset or "auto": try Docker, fall back to local + emit warning
    """
    global _executor
    if _executor is not None:
        return _executor

    backend = os.getenv("CODE_SANDBOX_BACKEND", "auto").lower()

    if backend == "docker":
        # Force Docker — let it raise if unavailable
        _executor = DockerExecutor()
        logger.info("code-sandbox backend: Docker")
    elif backend == "local":
        # Force local subprocess
        _executor = LocalSubprocessExecutor()
        logger.info("code-sandbox backend: local subprocess (unsandboxed)")
    else:
        # Auto-fallback: try Docker, fall back to local
        try:
            docker_exec = DockerExecutor()
            # Verify the daemon is reachable by pinging it
            docker_exec.client.ping()
            _executor = docker_exec
            logger.info("code-sandbox backend: Docker")
        except Exception as e:
            # Docker daemon is down or unavailable
            logger.warning(
                "Docker daemon unavailable (%s); falling back to local subprocess mode",
                e,
            )
            logger.warning(
                "=" * 80
            )
            logger.warning(
                "CODE-SANDBOX: AUTO-FALLBACK TO LOCAL, UNSANDBOXED MODE"
            )
            logger.warning(
                "No isolation (filesystem/network/PID). Resource limits via RLIMIT only."
            )
            logger.warning(
                "For TRUSTED code only. Deploy with Docker for production."
            )
            logger.warning(
                "=" * 80
            )
            _executor = LocalSubprocessExecutor()
            logger.info("code-sandbox backend: local subprocess (unsandboxed, fallback)")

    return _executor


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="run_python",
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
            name="run_shell",
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
            name="run_tests",
            description="Run a test suite in an isolated container. Returns JSON with "
            "passed, failed, errors, duration_ms, output, timed_out.",
            inputSchema={
                "type": "object",
                "properties": {
                    "test_path": {"type": "string"},
                    "framework": {"type": "string", "default": "pytest"},
                    "timeout": {"type": "integer", "default": 120},
                    "expr": {"type": "string"},
                },
                "required": ["test_path"],
            },
        ),
        Tool(
            name="install_packages",
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
        if name == "run_python":
            result = executor.run_python(
                arguments["code"],
                timeout=arguments.get("timeout", 30),
                packages=arguments.get("packages", []),
            )
        elif name == "run_shell":
            result = executor.run_shell(
                arguments["cmd"], timeout=arguments.get("timeout", 30)
            )
        elif name == "run_tests":
            result = executor.run_tests(
                arguments["test_path"],
                framework=arguments.get("framework", "pytest"),
                timeout=arguments.get("timeout", 120),
                expr=arguments.get("expr"),
            )
        elif name == "install_packages":
            result = executor.run_python(
                "print('packages installed')",
                packages=arguments["packages"],
            )
        else:
            raise ValueError(f"unknown tool: {name}")

        # VISIBILITY: if running in unsandboxed (local) mode, prepend a warning to the
        # result's output field. This makes the loss of isolation visible to the agent/user
        # without changing the backend. ExecutionResult carries output in "stdout";
        # TestResult carries it in "output" — prepend to whichever exists (no phantom key).
        result_dict = result.model_dump()
        if not result.sandboxed:
            warning_line = "[WARNING: unsandboxed local mode — no isolation]\n"
            target = "stdout" if "stdout" in result_dict else "output"
            result_dict[target] = warning_line + (result_dict.get(target) or "")

        return [TextContent(type="text", text=json.dumps(result_dict))]
    except Exception as e:
        logger.exception("tool %s failed", name)
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def main() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
