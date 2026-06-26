"""MCP stdio server for the test-gen skill.

CRITICAL: stdout is the JSON-RPC 2.0 channel. NEVER print() anywhere.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("test-gen.server")

from mutation_runner import MutationRunner, run_pytest  # noqa: E402
from test_generator import TestGenerator    # noqa: E402

app: Server = Server("test-gen")
_runner: MutationRunner | None = None
_generator: TestGenerator | None = None


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="generate",
            description=(
                "Generate a pytest unit test suite for a Python source file. "
                "Returns JSON with test_code and explanation."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_file": {"type": "string", "description": "Path to the Python source file."},
                    "existing_tests": {"type": "string", "description": "Existing test code to extend, optional.", "default": ""},
                },
                "required": ["source_file"],
            },
        ),
        Tool(
            name="run_tests",
            description=(
                "Re-run an EXISTING pytest test file as-is (plain `pytest`), WITHOUT "
                "regenerating tests. Use this when the test suite already exists and "
                "the source under test has not changed — to re-verify after a fix or "
                "just re-run the suite. Returns JSON with passed (bool), passed_count, "
                "failed_count, summary, raw_output. Do NOT call `generate` to re-run "
                "tests that already exist."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "test_file": {"type": "string", "description": "Path to the existing pytest test file (or tests dir)."},
                    "cwd": {"type": "string", "description": "Working directory to run pytest from, optional.", "default": ""},
                },
                "required": ["test_file"],
            },
        ),
        Tool(
            name="run_mutations",
            description=(
                "Run mutation testing (mutmut) on a source file with a test "
                "file. Returns JSON with mutation_score, surviving_mutants "
                "(diffs), killed_count, total_count."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_file": {"type": "string"},
                    "test_file": {"type": "string"},
                },
                "required": ["source_file", "test_file"],
            },
        ),
        Tool(
            name="improve",
            description=(
                "Given surviving mutant diffs, generate additional pytest tests "
                "targeting those fault classes. Returns JSON with "
                "additional_test_code."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_file": {"type": "string"},
                    "test_file": {"type": "string"},
                    "surviving_mutants": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Unified diffs of surviving mutants.",
                    },
                },
                "required": ["source_file", "test_file", "surviving_mutants"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    global _runner, _generator
    if _runner is None:
        _runner = MutationRunner()
    if _generator is None:
        _generator = TestGenerator()

    try:
        if name == "generate":
            payload = _generator.generate(
                source_file=arguments["source_file"],
                existing_tests=arguments.get("existing_tests", ""),
            )
        elif name == "run_tests":
            payload = run_pytest(
                test_file=arguments["test_file"],
                cwd=arguments.get("cwd") or None,
            )
        elif name == "run_mutations":
            result = _runner.run(
                source_file=arguments["source_file"],
                test_file=arguments["test_file"],
            )
            payload = result.to_dict()
        elif name == "improve":
            payload = _generator.improve(
                source_file=arguments["source_file"],
                test_file=arguments["test_file"],
                surviving_mutants=arguments["surviving_mutants"],
            )
        else:
            log.error("unknown tool: %s", name)
            return [TextContent(type="text", text=json.dumps({"error": f"unknown tool: {name}"}))]
    except Exception as exc:  # never crash the stdio server
        log.exception("tool %s failed", name)
        return [TextContent(type="text", text=json.dumps({"error": repr(exc)}))]

    return [TextContent(type="text", text=json.dumps(payload))]


async def main() -> None:
    log.info("starting test-gen MCP server")
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
