# figma-to-component MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the figma-to-component Python MCP server — Figma REST API structured data extraction + Gemma 4 React/Tailwind code synthesis from component spec.

**Architecture:** FigmaFetcher calls GET /v1/files/:key/nodes?ids=:node_id, parses the component tree into a typed FigmaNode hierarchy. ComponentSynthesizer serializes the spec to a structured prompt and calls Gemma 4 via litellm to generate the React component + TypeScript props interface. All LLM calls use GEMMA_BASE (single-GPU). All logging to stderr.

**Tech Stack:** Python 3.11+, `mcp` SDK, `httpx`, `litellm`, `pydantic>=2`, `pytest-asyncio`

---

## Critical constraints (apply to every task)

- **stdout is sacred.** All logging uses `logging` configured with `stream=sys.stderr`. NEVER `print()` anywhere. stdout carries JSON-RPC 2.0 framing; any stray byte corrupts the stream silently and produces misleading `Parse error` symptoms downstream. litellm and httpx can be chatty — they MUST be wired to stderr (the root logger configured below routes them there).
- **Single-GPU.** Every LLM call goes to `GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")` with model `GEMMA_MODEL = os.getenv("GEMMA_MODEL", "google/gemma-4-31B-it")`. There is **NO Qwen, NO QWEN_BASE** anywhere in this skill.
- **Never tiktoken.** If a token budget check is ever needed, use a character cap or `AutoTokenizer.from_pretrained("google/gemma-4-9b-it")`. Gemma uses SentencePiece; tiktoken counts are wrong.
- **Figma token required.** `FIGMA_ACCESS_TOKEN = os.getenv("FIGMA_ACCESS_TOKEN")`. Raise a clear, actionable error when missing — never silently produce empty output.
- **Structured data, not pixels.** This skill works from the Figma REST JSON (component tree, auto-layout, variables), NOT from screenshots. Screenshot-to-component is a separate skill; do not fetch images or render pixels here.
- The server is a **child process** spawned by the SkillRegistry over stdio. It must not assume a TTY, must not print banners, and returns tool results as JSON strings.

---

## Phase 0 — Scaffolding

### Task 0.1 — Create directory structure

- [ ] Create the skill server and test directories.

```bash
mkdir -p /Users/zachstallbohm/Work/gemma/services/skills/figma-to-component
mkdir -p /Users/zachstallbohm/Work/gemma/tests/services/skills/figma-to-component
```

### Task 0.2 — Write requirements.txt

- [ ] Create `services/skills/figma-to-component/requirements.txt`:

```text
mcp>=1.0.0
httpx>=0.27.0
litellm>=1.40.0
pydantic>=2
```

### Task 0.3 — Write SKILL.md

- [ ] Create `services/skills/figma-to-component/SKILL.md` (frontmatter exactly as below; body is model-agnostic markdown, no absolute paths):

```markdown
---
name: figma-to-component
description: >
  Converts a Figma component or frame into a React + Tailwind component using
  the Figma REST API for structured data extraction (not screenshot pixels).
  Higher fidelity than screenshot-to-component for well-structured Figma files
  with auto-layout and design variables. Requires FIGMA_ACCESS_TOKEN env var.
trigger: "Use when converting a specific Figma component or frame to React code"
tools:
  - figma_to_component.convert
  - figma_to_component.inspect
version: "0.1.0"
license: MIT
requires: [design-token-transform]
---

# figma-to-component

You have access to the `figma_to_component` MCP server. It fetches a Figma node's
structured data (component tree, auto-layout, fills, text styles, bound variables)
via the Figma REST API and uses Gemma 4 to synthesize a React + Tailwind component.

Because it reads structured data rather than rasterized pixels, it produces higher
fidelity output than screenshot-to-component for files that use auto-layout and
design variables.

## When to Use

- Converting a specific Figma component or frame to a React + Tailwind component
- Inspecting the structured spec of a Figma node before generating code

## Prerequisites

- `FIGMA_ACCESS_TOKEN` must be set (a Figma personal access token).
- You need the file key and node id. From a Figma URL
  `https://www.figma.com/file/<FILE_KEY>/...?node-id=<NODE_ID>`, the file key is the
  path segment and the node id is the `node-id` query parameter (decode `%3A` to `:`).

## Available Tools

### `figma_to_component.inspect(figma_file_key, node_id)`

Fetch and return the structured Figma node data as JSON (the `ComponentSpec`:
node tree, layout, fills, text styles, bound variables, referenced tokens). Use this
to inspect before generating.

### `figma_to_component.convert(figma_file_key, node_id, framework="react-tailwind")`

Fetch the node, synthesize code, and return JSON:

```json
{
  "component_code": "export function Card(...) { ... }",
  "component_name": "Card",
  "props_interface": "export interface CardProps { ... }",
  "framework": "react-tailwind"
}
```

## Limitations

- Fidelity depends on the Figma file using auto-layout and variables. Absolutely
  positioned, ungrouped layers translate poorly.
- v0.1.0 targets `react-tailwind` only; other frameworks raise an error.
- Generated code is a starting point; review styling against the design.
```

---

## Phase 1 — Shared types (Pydantic models)

### Task 1.1 — Create models.py with logging header and FigmaNode

- [ ] Create `services/skills/figma-to-component/models.py`. The module header documents the stdout rule; logging handlers are configured in `server.py`:

```python
"""Pydantic data models for the figma-to-component skill.

CRITICAL: this module is loaded inside an MCP stdio child process.
NEVER print() or write to stdout. All logging goes to sys.stderr.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class FigmaNode(BaseModel):
    id: str
    name: str
    type: str                              # FRAME, COMPONENT, TEXT, RECTANGLE, etc.
    layout: dict = Field(default_factory=dict)   # auto-layout: direction, padding, gap, align
    fills: list[dict] = Field(default_factory=list)   # color/gradient fills
    text_style: dict | None = None         # font family, size, weight, line height (TEXT only)
    children: list["FigmaNode"] = Field(default_factory=list)
    variables: dict = Field(default_factory=dict)     # bound design variables (varId -> field)


FigmaNode.model_rebuild()  # resolve the self-referential 'children' forward ref
```

### Task 1.2 — Add ComponentSpec and ComponentResult

- [ ] Append the spec and result models:

```python
class ComponentSpec(BaseModel):
    node: FigmaNode
    file_key: str
    node_id: str
    tokens: dict = Field(default_factory=dict)   # referenced design tokens (varId -> resolved value)


class ComponentResult(BaseModel):
    component_code: str
    component_name: str
    props_interface: str        # TypeScript interface for props
    framework: str
```

---

## Phase 2 — FigmaFetcher: REST API + node parsing

### Task 2.1 — Create figma_fetcher.py header, constants, and missing-token guard

- [ ] Create `services/skills/figma-to-component/figma_fetcher.py`. The Figma base URL and token are read from the environment; the token check raises a clear error:

```python
"""FigmaFetcher: fetch a Figma node via the REST API and parse it into a typed tree.

CRITICAL: stdout is the JSON-RPC channel. NEVER print(); log to sys.stderr only.
This skill works from STRUCTURED Figma JSON, not from screenshot pixels.
"""
from __future__ import annotations

import logging
import os

import httpx

from models import ComponentSpec, FigmaNode

log = logging.getLogger("figma-to-component.fetcher")  # handlers set in server.py

FIGMA_API_BASE = os.getenv("FIGMA_API_BASE", "https://api.figma.com")
DEFAULT_TIMEOUT_S = 30.0


class FigmaTokenMissingError(RuntimeError):
    """Raised when FIGMA_ACCESS_TOKEN is not configured."""


def _require_token() -> str:
    token = os.getenv("FIGMA_ACCESS_TOKEN")
    if not token:
        raise FigmaTokenMissingError(
            "FIGMA_ACCESS_TOKEN is not set. Create a Figma personal access token "
            "(Figma -> Settings -> Personal access tokens) and export it as "
            "FIGMA_ACCESS_TOKEN before using the figma-to-component skill."
        )
    return token
```

### Task 2.2 — FigmaFetcher.__init__

- [ ] Append the class constructor. The token is resolved eagerly so a missing token fails fast at construction with the actionable message:

```python
class FigmaFetcher:
    """Fetches and parses structured Figma node data via the REST API."""

    def __init__(self, token: str | None = None, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        # Resolve eagerly: fail fast with a clear error if the token is missing.
        self._token = token or _require_token()
        self._timeout = timeout
```

### Task 2.3 — get_node: call the nodes endpoint, build a ComponentSpec

- [ ] Append the public async fetch. It calls `GET /v1/files/:key/nodes?ids=:node_id`, extracts the requested node's document, parses it, and resolves referenced variables into `tokens`:

```python
    async def get_node(self, file_key: str, node_id: str) -> ComponentSpec:
        url = f"{FIGMA_API_BASE}/v1/files/{file_key}/nodes"
        headers = {"X-Figma-Token": self._token}
        params = {"ids": node_id}

        log.info("fetching figma node file=%s node=%s", file_key, node_id)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            payload = resp.json()

        nodes = payload.get("nodes") or {}
        entry = nodes.get(node_id)
        if not entry or "document" not in entry:
            raise ValueError(
                f"node {node_id!r} not found in file {file_key!r} "
                f"(returned keys: {list(nodes)})"
            )

        document = entry["document"]
        node = self._parse_node(document)
        tokens = self._collect_tokens(node)
        return ComponentSpec(
            node=node,
            file_key=file_key,
            node_id=node_id,
            tokens=tokens,
        )
```

### Task 2.4 — _parse_node: map raw Figma JSON to FigmaNode (recursive)

- [ ] Append the recursive parser. It maps Figma node types and pulls auto-layout, fills, text style, and bound variables. Children are parsed recursively. All field access is defensive so an unexpected shape degrades to defaults rather than crashing:

```python
    def _parse_node(self, raw: dict) -> FigmaNode:
        return FigmaNode(
            id=raw.get("id", ""),
            name=raw.get("name", ""),
            type=raw.get("type", "UNKNOWN"),
            layout=self._parse_layout(raw),
            fills=self._parse_fills(raw),
            text_style=self._parse_text_style(raw),
            variables=self._parse_variables(raw),
            children=[self._parse_node(c) for c in raw.get("children", [])],
        )

    @staticmethod
    def _parse_layout(raw: dict) -> dict:
        # Auto-layout fields live at the node top level in the Figma REST schema.
        mode = raw.get("layoutMode")  # "HORIZONTAL" | "VERTICAL" | None (no auto-layout)
        if not mode:
            return {}
        return {
            "direction": mode,
            "gap": raw.get("itemSpacing", 0),
            "padding": {
                "top": raw.get("paddingTop", 0),
                "right": raw.get("paddingRight", 0),
                "bottom": raw.get("paddingBottom", 0),
                "left": raw.get("paddingLeft", 0),
            },
            "align_items": raw.get("counterAxisAlignItems"),
            "justify_content": raw.get("primaryAxisAlignItems"),
        }

    @staticmethod
    def _parse_fills(raw: dict) -> list[dict]:
        fills = raw.get("fills")
        return fills if isinstance(fills, list) else []

    @staticmethod
    def _parse_text_style(raw: dict) -> dict | None:
        if raw.get("type") != "TEXT":
            return None
        style = raw.get("style") or {}
        return {
            "characters": raw.get("characters", ""),
            "font_family": style.get("fontFamily"),
            "font_size": style.get("fontSize"),
            "font_weight": style.get("fontWeight"),
            "line_height_px": style.get("lineHeightPx"),
            "text_align": style.get("textAlignHorizontal"),
        }

    @staticmethod
    def _parse_variables(raw: dict) -> dict:
        # boundVariables maps a node field (e.g. "fills") to a variable alias { id, type }.
        bound = raw.get("boundVariables")
        return bound if isinstance(bound, dict) else {}
```

### Task 2.5 — _collect_tokens: gather referenced variable ids across the tree

- [ ] Append a helper that walks the parsed tree and collects every bound-variable id it references. v0.1.0 records the ids/aliases as the `tokens` map (resolved values are the job of `design-token-transform`, the declared prerequisite):

```python
    def _collect_tokens(self, node: FigmaNode) -> dict:
        tokens: dict = {}

        def walk(n: FigmaNode) -> None:
            for field, alias in (n.variables or {}).items():
                # alias is typically {"id": "VariableID:...", "type": "VARIABLE_ALIAS"}
                if isinstance(alias, dict) and alias.get("id"):
                    tokens[alias["id"]] = {"field": field, **alias}
            for child in n.children:
                walk(child)

        walk(node)
        return tokens
```

> **Implementer note:** resolving variable ids to concrete values requires `GET /v1/files/:key/variables/local` (Figma Enterprise) or the `design-token-transform` skill. v0.1.0 surfaces the referenced ids so the synthesizer can prompt with token names; full resolution is a follow-up.

---

## Phase 3 — ComponentSynthesizer: Gemma 4 code synthesis

### Task 3.1 — Create component_synth.py header, constants, helpers

- [ ] Create `services/skills/figma-to-component/component_synth.py`. LLM config is read from the environment (single-GPU: GEMMA only):

```python
"""ComponentSynthesizer: serialize a ComponentSpec to a prompt and call Gemma 4
via litellm to synthesize a React + Tailwind component.

CRITICAL: stdout is the JSON-RPC channel. NEVER print(); log to sys.stderr only.
Single-GPU: all LLM calls target GEMMA_BASE. There is no QWEN_BASE here.
"""
from __future__ import annotations

import json
import logging
import os
import re

import litellm

from models import ComponentResult, ComponentSpec

log = logging.getLogger("figma-to-component.synth")

GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")
GEMMA_MODEL = os.getenv("GEMMA_MODEL", "google/gemma-4-31B-it")

SUPPORTED_FRAMEWORKS = {"react-tailwind"}
```

### Task 3.2 — _to_pascal_case and _component_name_from_spec

- [ ] Append helpers that derive a valid React component name from the Figma node name:

```python
def _to_pascal_case(name: str) -> str:
    parts = re.split(r"[^0-9a-zA-Z]+", name)
    pascal = "".join(p[:1].upper() + p[1:] for p in parts if p)
    if not pascal or not pascal[0].isalpha():
        pascal = "Component" + pascal
    return pascal
```

### Task 3.3 — _build_prompt: serialize spec into a structured synthesis prompt

- [ ] Append the prompt builder. It MUST include auto-layout info (direction, gap, padding), fills, text styles, and referenced tokens so the model can faithfully translate structure to Tailwind classes:

```python
class ComponentSynthesizer:
    """Synthesizes a React + Tailwind component from a ComponentSpec via Gemma 4."""

    def _build_prompt(self, spec: ComponentSpec, component_name: str) -> str:
        # Serialize the structured tree compactly. Auto-layout must be present so the
        # model maps Figma direction/gap/padding to flexbox Tailwind utilities.
        node_json = spec.node.model_dump_json(indent=2)
        tokens_json = json.dumps(spec.tokens, indent=2)
        return f"""You are an expert React + Tailwind CSS engineer. Convert the following
STRUCTURED Figma node into a single React function component using Tailwind CSS.

Rules:
- Use the auto-layout fields (direction, gap, padding, align) to choose flexbox
  Tailwind utilities (flex, flex-col, gap-*, p-*, items-*, justify-*).
- Map fills to Tailwind color/background utilities; map text_style to font utilities.
- Prefer referenced design tokens (by name) over hardcoded values where present.
- The component MUST be named exactly `{component_name}`.
- Emit a TypeScript props interface named `{component_name}Props` for any text or
  configurable content (each TEXT node's characters should be a prop).

Return ONLY a JSON object with this exact shape, no prose, no code fences:
{{
  "component_code": "<the full .tsx component source>",
  "props_interface": "<the exported TypeScript interface source>"
}}

REFERENCED DESIGN TOKENS:
{tokens_json}

FIGMA NODE (structured, includes auto-layout):
{node_json}
"""
```

### Task 3.4 — _call_llm: litellm → GEMMA_BASE

- [ ] Append the LLM call. Mirrors the canonical litellm pattern used across the skills (model prefixed `openai/`, `api_base=GEMMA_BASE`):

```python
    def _call_llm(self, prompt: str) -> str:
        resp = litellm.completion(
            model=f"openai/{GEMMA_MODEL}",
            api_base=GEMMA_BASE,
            api_key="not-needed",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        return resp["choices"][0]["message"]["content"]
```

### Task 3.5 — _parse_synth_json: tolerant JSON extraction

- [ ] Append a tolerant parser that strips code fences and locates the outermost JSON object (LLM output is non-deterministic):

```python
    @staticmethod
    def _parse_synth_json(raw: str) -> dict:
        s = raw.strip()
        if s.startswith("```"):
            s = s.split("```", 2)[1]
            if s.startswith("json"):
                s = s[4:]
        start, end = s.find("{"), s.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("no JSON object found in synthesis output")
        return json.loads(s[start : end + 1])
```

### Task 3.6 — Public synthesize()

- [ ] Append the public method. It validates the framework, derives the name, builds the prompt, calls the LLM, parses the result, and returns a `ComponentResult`:

```python
    def synthesize(self, spec: ComponentSpec, framework: str) -> ComponentResult:
        if framework not in SUPPORTED_FRAMEWORKS:
            raise ValueError(
                f"unsupported framework {framework!r}; "
                f"supported: {sorted(SUPPORTED_FRAMEWORKS)}"
            )
        component_name = _to_pascal_case(spec.node.name or "Component")
        prompt = self._build_prompt(spec, component_name)
        raw = self._call_llm(prompt)
        parsed = self._parse_synth_json(raw)
        return ComponentResult(
            component_code=parsed.get("component_code", ""),
            component_name=component_name,
            props_interface=parsed.get("props_interface", ""),
            framework=framework,
        )
```

---

## Phase 4 — MCP server entry point

### Task 4.1 — Create server.py with stderr logging, imports, app

- [ ] Create `services/skills/figma-to-component/server.py`. Logging is configured to stderr FIRST, before importing modules that may log (litellm, httpx):

```python
"""MCP stdio server for the figma_to_component skill.

CRITICAL: stdout is the JSON-RPC 2.0 channel. NEVER print() anywhere.
All logging is configured to sys.stderr below, including third-party loggers.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys

# CRITICAL: stderr only. Configure before importing anything that may log.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("figma-to-component.server")

from mcp.server import Server  # noqa: E402 (after logging is configured)
from mcp.server.stdio import stdio_server  # noqa: E402
from mcp.types import TextContent, Tool  # noqa: E402

from figma_fetcher import FigmaFetcher  # noqa: E402
from component_synth import ComponentSynthesizer  # noqa: E402

app: Server = Server("figma-to-component")
```

### Task 4.2 — Implement list_tools

- [ ] Append the `list_tools` handler exposing both tools with self-contained JSON Schemas. Tool names are `figma_to_component.convert` and `figma_to_component.inspect`:

```python
@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="figma_to_component.convert",
            description=(
                "Fetch a Figma node's structured data and synthesize a React + Tailwind "
                "component. Returns JSON: component_code, component_name, props_interface, "
                "framework."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "figma_file_key": {"type": "string", "description": "Figma file key."},
                    "node_id": {"type": "string", "description": "Figma node id (e.g. '1:23')."},
                    "framework": {
                        "type": "string",
                        "default": "react-tailwind",
                        "description": "Target framework. v0.1.0 supports 'react-tailwind'.",
                    },
                },
                "required": ["figma_file_key", "node_id"],
            },
        ),
        Tool(
            name="figma_to_component.inspect",
            description=(
                "Fetch and return the structured Figma node data (ComponentSpec) as JSON, "
                "for inspection before generating code."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "figma_file_key": {"type": "string", "description": "Figma file key."},
                    "node_id": {"type": "string", "description": "Figma node id (e.g. '1:23')."},
                },
                "required": ["figma_file_key", "node_id"],
            },
        ),
    ]
```

### Task 4.3 — Implement call_tool

- [ ] Append the `call_tool` dispatcher. It lazily constructs the fetcher/synthesizer (so a missing FIGMA_ACCESS_TOKEN surfaces as a tool-level error message), routes by name, and serializes results to JSON. Errors are returned as content rather than crashing the stdio server:

```python
_fetcher: FigmaFetcher | None = None
_synth: ComponentSynthesizer | None = None


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    global _fetcher, _synth
    try:
        if _fetcher is None:
            _fetcher = FigmaFetcher()      # raises FigmaTokenMissingError if no token
        if _synth is None:
            _synth = ComponentSynthesizer()

        if name == "figma_to_component.inspect":
            spec = await _fetcher.get_node(arguments["figma_file_key"], arguments["node_id"])
            text = spec.model_dump_json()
        elif name == "figma_to_component.convert":
            spec = await _fetcher.get_node(arguments["figma_file_key"], arguments["node_id"])
            framework = arguments.get("framework", "react-tailwind")
            result = _synth.synthesize(spec, framework)
            text = result.model_dump_json()
        else:
            log.error("unknown tool: %s", name)
            return [TextContent(type="text", text=json.dumps({"error": f"unknown tool: {name}"}))]
    except Exception as exc:  # never let one bad call crash the stdio server
        log.exception("tool %s failed", name)
        return [TextContent(type="text", text=json.dumps({"error": repr(exc)}))]

    return [TextContent(type="text", text=text)]
```

### Task 4.4 — Implement main and entry guard

- [ ] Append the async `main` and the `__main__` guard:

```python
async def main() -> None:
    log.info("starting figma-to-component MCP server")
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Phase 5 — Tests (mocked, no GPU, no network)

### Task 5.1 — Create conftest.py with sys.path, a raw Figma fixture, and a fake litellm

- [ ] Create `tests/services/skills/figma-to-component/conftest.py`. It puts the server dir on `sys.path`, provides a raw Figma `nodes` response fixture (a FRAME with auto-layout containing a TEXT and a RECTANGLE child), and a fake litellm response factory:

```python
import sys
from pathlib import Path

import pytest

SERVER_DIR = (
    Path(__file__).resolve().parents[4]
    / "services" / "skills" / "figma-to-component"
)
sys.path.insert(0, str(SERVER_DIR))


@pytest.fixture
def raw_node_document() -> dict:
    """A Figma FRAME (auto-layout) with a TEXT child and a RECTANGLE child."""
    return {
        "id": "1:2",
        "name": "Primary Card",
        "type": "FRAME",
        "layoutMode": "VERTICAL",
        "itemSpacing": 8,
        "paddingTop": 16,
        "paddingRight": 16,
        "paddingBottom": 16,
        "paddingLeft": 16,
        "counterAxisAlignItems": "CENTER",
        "primaryAxisAlignItems": "MIN",
        "fills": [{"type": "SOLID", "color": {"r": 1, "g": 1, "b": 1}}],
        "boundVariables": {"fills": {"id": "VariableID:9:9", "type": "VARIABLE_ALIAS"}},
        "children": [
            {
                "id": "1:3",
                "name": "Title",
                "type": "TEXT",
                "characters": "Hello",
                "style": {
                    "fontFamily": "Inter",
                    "fontSize": 18,
                    "fontWeight": 600,
                    "lineHeightPx": 24,
                    "textAlignHorizontal": "LEFT",
                },
                "fills": [{"type": "SOLID", "color": {"r": 0, "g": 0, "b": 0}}],
            },
            {
                "id": "1:4",
                "name": "Divider",
                "type": "RECTANGLE",
                "fills": [{"type": "SOLID", "color": {"r": 0.9, "g": 0.9, "b": 0.9}}],
            },
        ],
    }


@pytest.fixture
def nodes_response(raw_node_document) -> dict:
    """Shape returned by GET /v1/files/:key/nodes?ids=:id."""
    return {"nodes": {"1:2": {"document": raw_node_document}}}


@pytest.fixture
def fake_synth_payload() -> str:
    """A well-formed JSON string as Gemma 4 would return it."""
    import json
    return json.dumps(
        {
            "component_code": (
                "export function PrimaryCard({ title }: PrimaryCardProps) {\n"
                "  return <div className=\"flex flex-col gap-2 p-4 items-center\">"
                "<span>{title}</span></div>;\n}"
            ),
            "props_interface": "export interface PrimaryCardProps { title: string; }",
        }
    )
```

### Task 5.2 — test_figma_fetcher: missing token raises a clear error

- [ ] Create `tests/services/skills/figma-to-component/test_figma_fetcher.py`. First test asserts the actionable error when the env var is absent:

```python
import pytest


@pytest.mark.mocked
def test_missing_token_raises_clear_error(monkeypatch):
    monkeypatch.delenv("FIGMA_ACCESS_TOKEN", raising=False)
    from figma_fetcher import FigmaFetcher, FigmaTokenMissingError

    with pytest.raises(FigmaTokenMissingError) as exc:
        FigmaFetcher()
    msg = str(exc.value)
    assert "FIGMA_ACCESS_TOKEN" in msg
    assert "token" in msg.lower()
```

### Task 5.3 — test_figma_fetcher: _parse_node maps FRAME/COMPONENT/TEXT correctly

- [ ] Append. Verify type mapping, auto-layout extraction, text style, fills, recursion, and bound variables:

```python
@pytest.mark.mocked
def test_parse_node_maps_types_and_layout(monkeypatch, raw_node_document):
    monkeypatch.setenv("FIGMA_ACCESS_TOKEN", "tok")
    from figma_fetcher import FigmaFetcher

    fetcher = FigmaFetcher()
    node = fetcher._parse_node(raw_node_document)

    assert node.type == "FRAME"
    assert node.name == "Primary Card"
    # Auto-layout mapped
    assert node.layout["direction"] == "VERTICAL"
    assert node.layout["gap"] == 8
    assert node.layout["padding"]["top"] == 16
    # Children recursed and typed
    assert [c.type for c in node.children] == ["TEXT", "RECTANGLE"]
    # TEXT child carries text_style; RECTANGLE does not
    text_child = node.children[0]
    assert text_child.text_style is not None
    assert text_child.text_style["font_size"] == 18
    assert node.children[1].text_style is None
    # Fills and bound variables captured
    assert node.fills and node.fills[0]["type"] == "SOLID"
    assert node.variables["fills"]["id"] == "VariableID:9:9"


@pytest.mark.mocked
def test_parse_node_component_type(monkeypatch):
    monkeypatch.setenv("FIGMA_ACCESS_TOKEN", "tok")
    from figma_fetcher import FigmaFetcher

    node = FigmaFetcher()._parse_node({"id": "x", "name": "Btn", "type": "COMPONENT"})
    assert node.type == "COMPONENT"
    assert node.layout == {}   # no auto-layout fields -> empty
```

### Task 5.4 — test_figma_fetcher: get_node builds a ComponentSpec (mock httpx)

- [ ] Append. Mock `httpx.AsyncClient` so no network call happens; assert the spec carries the file key, node id, parsed node, and collected tokens:

```python
@pytest.mark.mocked
@pytest.mark.asyncio
async def test_get_node_builds_spec(monkeypatch, nodes_response):
    monkeypatch.setenv("FIGMA_ACCESS_TOKEN", "tok")
    import figma_fetcher

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return nodes_response

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, params=None):
            assert headers["X-Figma-Token"] == "tok"
            assert params["ids"] == "1:2"
            return _FakeResp()

    monkeypatch.setattr(figma_fetcher.httpx, "AsyncClient", _FakeClient)

    fetcher = figma_fetcher.FigmaFetcher()
    spec = await fetcher.get_node("FILEKEY", "1:2")

    assert spec.file_key == "FILEKEY"
    assert spec.node_id == "1:2"
    assert spec.node.type == "FRAME"
    # token id collected from the bound variable
    assert "VariableID:9:9" in spec.tokens
```

### Task 5.5 — test_component_synth: prompt includes auto-layout info

- [ ] Create `tests/services/skills/figma-to-component/test_component_synth.py`. Build a spec from the fixture and assert the synthesis prompt contains the auto-layout direction, gap, and padding:

```python
import pytest


def _spec_from_fixture(raw_node_document):
    import figma_fetcher
    from models import ComponentSpec

    node = figma_fetcher.FigmaFetcher.__new__(figma_fetcher.FigmaFetcher)._parse_node(
        raw_node_document
    )
    return ComponentSpec(node=node, file_key="K", node_id="1:2", tokens={})


@pytest.mark.mocked
def test_prompt_includes_auto_layout(raw_node_document):
    from component_synth import ComponentSynthesizer

    spec = _spec_from_fixture(raw_node_document)
    prompt = ComponentSynthesizer()._build_prompt(spec, "PrimaryCard")

    assert "VERTICAL" in prompt          # direction
    assert "auto-layout" in prompt.lower()
    assert "gap" in prompt.lower()
    assert "padding" in prompt.lower() or "paddingTop" in prompt
    assert "PrimaryCard" in prompt
```

> `FigmaFetcher.__new__(...)` is used to parse without resolving the token in this pure-parsing test. If preferred, set `FIGMA_ACCESS_TOKEN` via monkeypatch and construct normally.

### Task 5.6 — test_component_synth: synthesize returns code + props_interface

- [ ] Append. Mock `litellm.completion` to return the fake payload; assert the `ComponentResult` is populated and the name is PascalCase from the node name:

```python
@pytest.mark.mocked
def test_synthesize_returns_result(monkeypatch, raw_node_document, fake_synth_payload):
    import component_synth

    monkeypatch.setattr(
        component_synth.litellm,
        "completion",
        lambda **k: {"choices": [{"message": {"content": fake_synth_payload}}]},
    )

    spec = _spec_from_fixture(raw_node_document)
    result = component_synth.ComponentSynthesizer().synthesize(spec, "react-tailwind")

    assert result.framework == "react-tailwind"
    assert result.component_name == "PrimaryCard"   # PascalCase from "Primary Card"
    assert "export function PrimaryCard" in result.component_code
    assert "PrimaryCardProps" in result.props_interface


@pytest.mark.mocked
def test_synthesize_rejects_unsupported_framework(raw_node_document):
    from component_synth import ComponentSynthesizer

    spec = _spec_from_fixture(raw_node_document)
    with pytest.raises(ValueError):
        ComponentSynthesizer().synthesize(spec, "vue")


@pytest.mark.mocked
def test_synthesize_uses_gemma_base(monkeypatch, raw_node_document, fake_synth_payload):
    """The LLM call must target GEMMA_BASE — never a Qwen endpoint."""
    import component_synth

    captured = {}

    def _fake_completion(**kwargs):
        captured.update(kwargs)
        return {"choices": [{"message": {"content": fake_synth_payload}}]}

    monkeypatch.setattr(component_synth.litellm, "completion", _fake_completion)
    spec = _spec_from_fixture(raw_node_document)
    component_synth.ComponentSynthesizer().synthesize(spec, "react-tailwind")

    assert captured["api_base"] == component_synth.GEMMA_BASE
    assert "gemma" in captured["model"].lower()
```

### Task 5.7 — test: nothing is written to stdout

- [ ] Append to `test_component_synth.py` (or a small `test_stdout.py`). Capture stdout around a full parse + synthesize and assert it stays empty:

```python
@pytest.mark.mocked
def test_no_stdout_during_synthesis(monkeypatch, raw_node_document, fake_synth_payload, capsys):
    import component_synth

    monkeypatch.setattr(
        component_synth.litellm,
        "completion",
        lambda **k: {"choices": [{"message": {"content": fake_synth_payload}}]},
    )
    spec = _spec_from_fixture(raw_node_document)
    component_synth.ComponentSynthesizer().synthesize(spec, "react-tailwind")

    captured = capsys.readouterr()
    assert captured.out == ""   # stdout is sacred
```

### Task 5.8 — Run the suite

- [ ] Run only the mocked tests and confirm green:

```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest tests/services/skills/figma-to-component -m mocked -v
```

---

## Phase 6 — Verification checklist

- [ ] Grep the server dir for stdout violations — must return nothing:

```bash
grep -rnE '(^|[^.])\bprint\(' /Users/zachstallbohm/Work/gemma/services/skills/figma-to-component
```

- [ ] Grep for forbidden tiktoken — must return nothing:

```bash
grep -rn 'tiktoken' /Users/zachstallbohm/Work/gemma/services/skills/figma-to-component
```

- [ ] Grep for any Qwen reference — must return nothing (single-GPU, GEMMA only):

```bash
grep -rni 'qwen' /Users/zachstallbohm/Work/gemma/services/skills/figma-to-component
```

- [ ] Confirm the skill reads structured data, not screenshots — there must be no image/render endpoint usage:

```bash
grep -rni 'images\|screenshot\|render\|png\|/v1/images' /Users/zachstallbohm/Work/gemma/services/skills/figma-to-component || echo "OK: no pixel/screenshot paths"
```

- [ ] Smoke-test the server starts and speaks JSON-RPC over stdio (initialize handshake). It should print nothing to stdout except framed JSON, and `tools/list` should return both tools:

```bash
cd /Users/zachstallbohm/Work/gemma/services/skills/figma-to-component && \
FIGMA_ACCESS_TOKEN=dummy python - <<'PY'
import asyncio, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(command=sys.executable, args=["server.py"])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print("TOOLS:", [t.name for t in tools.tools], file=sys.stderr)

asyncio.run(main())
PY
```

---

## Tool / requirement coverage map (self-review)

| Requirement | Task |
|---|---|
| `figma_to_component.convert` tool | 3.6, 4.2, 4.3, 5.6 |
| `figma_to_component.inspect` tool | 2.3, 4.2, 4.3, 5.4 |
| FigmaNode / ComponentSpec / ComponentResult models | 1.1, 1.2 |
| FigmaFetcher.get_node (REST: /v1/files/:key/nodes?ids=) | 2.3, 5.4 |
| FigmaFetcher._parse_node maps FRAME/COMPONENT/TEXT | 2.4, 5.3 |
| Auto-layout / fills / text_style / variables extraction | 2.4, 2.5, 5.3 |
| Clear error when FIGMA_ACCESS_TOKEN missing | 2.1, 2.2, 5.2 |
| ComponentSynthesizer.synthesize -> ComponentResult | 3.6, 5.6 |
| Synthesis prompt includes auto-layout info | 3.3, 5.5 |
| ComponentResult has component_code + props_interface | 1.2, 3.6, 5.6 |
| httpx for Figma REST | 2.3, 5.4 |
| litellm -> GEMMA_BASE (single-GPU, no QWEN_BASE) | 3.1, 3.4, 5.6, Phase 6 |
| Structured data, not screenshots | 2.x, Phase 6 |
| stderr-only logging, never stdout | 1.1, 2.1, 3.1, 4.1, 5.7, Phase 6 |
| No tiktoken | Phase 6 |
| SKILL.md frontmatter (exact) | 0.3 |
| stdio transport child process | 4.1, 4.4, Phase 6 |
| requires: design-token-transform | 0.3, 2.5 (note) |

---

## Notes for the implementer

- **MCP SDK version drift.** This plan uses the low-level `Server` + `@app.list_tools()` / `@app.call_tool()` API (same as pdf-parse). If the installed `@modelcontextprotocol` / `mcp` package differs, confirm import paths against `research/llm-harness-research/specs/spec_mcp_bridge.md` and `spec_skills.md` and the sibling skills already in `services/skills/`.
- **Variable resolution is out of scope for v0.1.0.** `_collect_tokens` records referenced variable ids/aliases only. Resolving them to concrete values needs `GET /v1/files/:key/variables/local` (Figma Enterprise) or the declared `design-token-transform` prerequisite. The synthesizer prompts with the referenced token ids so the contract is stable for later resolution.
- **Node id format.** Figma node ids use a colon (e.g. `1:23`) but appear url-encoded as `1%3A23` in file URLs. The skill expects the decoded form; document this in SKILL.md (done) and pass through unchanged to the `ids` query param.
- **Framework scope.** v0.1.0 supports `react-tailwind` only; `synthesize` raises `ValueError` for anything else. Adding `react-css-modules` or `vue` later means extending `SUPPORTED_FRAMEWORKS` and the prompt template — no schema change to `ComponentResult`.
- **No token counting today.** If a future change truncates large node trees to a context budget, use a char cap or `AutoTokenizer.from_pretrained("google/gemma-4-9b-it")`, never tiktoken.
- **litellm/httpx logging.** `basicConfig(stream=sys.stderr, ...)` in `server.py` routes the root logger (and therefore litellm/httpx) to stderr. Keep that call as the first executable statement before any third-party import that logs at import time.
```
