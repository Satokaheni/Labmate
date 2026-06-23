"""ast.search MCP server (stdio transport).

stdout is sacred — it carries JSON-RPC 2.0. All logging goes to sys.stderr.
NEVER call print() in this module.
"""

import asyncio
import json
import logging
import sys
from dataclasses import asdict

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from searcher import AstSearcher

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("ast.search.server")

app = Server("ast.search")
searcher = AstSearcher()


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="find_code",
            description=(
                "Find all AST nodes matching a structural pattern in a file or directory. "
                "Meta-variables: $VAR (single node), $$$MULTI (zero-or-more). "
                "Example pattern: requests.get($URL). Matches AST nodes only — never "
                "inside string literals or comments."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "ast-grep pattern."},
                    "language": {
                        "type": "string",
                        "description": "python | typescript | javascript | rust | go",
                    },
                    "path": {"type": "string", "description": "File or directory path."},
                },
                "required": ["pattern", "language", "path"],
            },
        ),
        Tool(
            name="rewrite",
            description=(
                "Rewrite nodes matching `pattern` to `replacement`. Returns a unified diff "
                "for review — NEVER writes to disk. Always preview before applying."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "replacement": {"type": "string"},
                    "language": {
                        "type": "string",
                        "description": "python | typescript | javascript | rust | go",
                    },
                    "path": {"type": "string"},
                },
                "required": ["pattern", "replacement", "language", "path"],
            },
        ),
        Tool(
            name="find_by_rule",
            description=(
                "Find nodes via a YAML rule supporting pattern, kind, inside, has, not "
                "constraints. The YAML must include top-level 'language' and 'rule' fields."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "rule_yaml": {"type": "string", "description": "ast-grep YAML rule."},
                    "path": {"type": "string"},
                },
                "required": ["rule_yaml", "path"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    log.info("call_tool %s args=%s", name, list(arguments))
    try:
        if name == "find_code":
            result = searcher.find_code(
                pattern=arguments["pattern"],
                language=arguments["language"],
                path=arguments["path"],
            )
            payload = [asdict(m) for m in result]
        elif name == "rewrite":
            diff = searcher.rewrite(
                pattern=arguments["pattern"],
                replacement=arguments["replacement"],
                language=arguments["language"],
                path=arguments["path"],
            )
            payload = asdict(diff)
        elif name == "find_by_rule":
            result = searcher.find_by_rule(
                rule_yaml=arguments["rule_yaml"],
                path=arguments["path"],
            )
            payload = [asdict(m) for m in result]
        else:
            raise ValueError(f"Unknown tool: {name}")
    except Exception as exc:  # noqa: BLE001 — surface error to model, keep stream clean
        log.exception("tool %s failed", name)
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    return [TextContent(type="text", text=json.dumps(payload, indent=2))]


async def main() -> None:
    log.info("ast.search MCP server starting (stdio)")
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
