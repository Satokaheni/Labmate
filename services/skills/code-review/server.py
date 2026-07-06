"""MCP server for the code-review skill (stdio JSON-RPC).

CRITICAL: stdout carries JSON-RPC. All logging goes to stderr.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

import instructor
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
from reviewer import CodeReviewer

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("code_review.server")

GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")
THINKING_BUDGET = int(os.getenv("CODE_REVIEW_THINKING_BUDGET", "0"))
MAX_RETRIES = int(os.getenv("CODE_REVIEW_MAX_RETRIES", "2"))

# Route the skill's model call through the SHARED resilient client
# (services.model_client.resilient_completion): cross-endpoint failover + the same
# transient-retry / thinking_budget self-heal the orchestrator uses, instead of a
# naked litellm.completion that dies on the first connection blip. Available in the
# harness (skill_registry puts the repo root on PYTHONPATH); fall back to raw
# litellm when the skill is run standalone (e.g. its own unit tests).
try:
    from services.model_client import resilient_completion as _COMPLETION
except ImportError:  # pragma: no cover — standalone skill run, no repo on path
    import litellm

    _COMPLETION = litellm.completion


class _GemmaClient:
    def chat(self, *, response_model, messages: list[dict], temperature: float = 0.2):
        client = instructor.from_litellm(_COMPLETION)
        return client.chat.completions.create(
            model="openai/gemma-4-31b",
            messages=messages,
            response_model=response_model,
            temperature=temperature,
            api_base=GEMMA_BASE,
            api_key="not-needed",
            max_retries=MAX_RETRIES,
            extra_body={"thinking_budget_tokens": THINKING_BUDGET},
        )


_reviewer = CodeReviewer(_GemmaClient())
app: Server = Server("code-review")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="code_review",
            description=(
                "Run multi-angle adversarial code review on a git diff or file/directory. "
                "Runs 5 analysis angles (correctness, security, removed behavior, language "
                "pitfalls, wrapper correctness), verifies each candidate, does a gap sweep, "
                "and returns findings ranked by severity. Use when asked to review code, "
                "find bugs, audit a PR diff, or check a file for issues."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "diff": {
                        "type": "string",
                        "description": "Git diff output (e.g. from `git diff HEAD~1`)",
                    },
                    "path": {
                        "type": "string",
                        "description": "File or directory path to review",
                    },
                    "k": {
                        "type": "integer",
                        "default": 15,
                        "description": "Max findings to return (default 15)",
                    },
                },
            },
        )
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name != "code_review":
            raise ValueError(f"unknown tool: {name}")

        diff: str | None = arguments.get("diff")
        path: str | None = arguments.get("path")
        k: int = int(arguments.get("k", 15))

        if not diff and not path:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": "provide 'diff' or 'path'"}),
                )
            ]

        def _run() -> dict:
            result = _reviewer.review(diff=diff, path=path, k=k)
            return {
                "findings": [f.model_dump() for f in result.findings],
                "lint_issues": result.lint_issues,
                "angles_run": result.angles_run,
                "total": len(result.findings),
            }

        result = await asyncio.to_thread(_run)
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    except Exception as exc:
        log.exception("code_review tool failed")
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]


async def main() -> None:
    async with stdio_server() as (r, w):
        await app.run(r, w, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
