# design-token-transform MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the design-token-transform Python MCP server — Figma REST API token extraction with transformation to Tailwind config, CSS variables, and shadcn/ui format.

**Architecture:** FigmaClient uses httpx to call the Figma REST API (GET /v1/files/:key), traverses the node tree to extract DesignToken objects (colors, typography, spacing). TokenTransformer converts the TokenSet to the requested format string. FIGMA_ACCESS_TOKEN from env. All logging to stderr.

**Tech Stack:** Python 3.11+, `mcp` SDK, `httpx`, `pydantic>=2`, `pytest-asyncio`

---

## Critical constraints (apply to every task)

- **stdout is sacred.** All logging uses `logging` configured with `stream=sys.stderr`. NEVER `print()`. stdout carries JSON-RPC 2.0 framing; any stray byte corrupts the stream silently and produces misleading `Parse error` symptoms downstream.
- **Never tiktoken.** This skill counts no tokens, but if that ever changes use `AutoTokenizer.from_pretrained("google/gemma-4-9b-it")` (SentencePiece), never tiktoken.
- **Fully deterministic.** No LLM inference anywhere. Given the same Figma response, output is byte-stable. Iterate dicts in a defined order (sorted by token name within category) so transforms are reproducible.
- **No hardcoded design values.** Every color, font size, spacing, and radius comes from the Figma API response. The only literals allowed are format scaffolding (`:root {`, `theme.extend`, etc.).
- **Secrets from env.** `FIGMA_ACCESS_TOKEN = os.getenv("FIGMA_ACCESS_TOKEN")` is required; raise a clear error if missing. `FIGMA_API_BASE = os.getenv("FIGMA_API_BASE", "https://api.figma.com/v1")`.
- **Async HTTP via httpx.** Use `httpx.AsyncClient`, never `requests`. Send the token as the `X-Figma-Token` header.
- The server is a **child process** spawned by the SkillRegistry over stdio. It must not assume a TTY, must not print banners, and must return tool errors as content rather than crashing.

---

## File structure (target)

```
services/skills/design-token-transform/
  server.py          # MCP server entry point
  figma_client.py    # FigmaClient class — REST API wrapper
  transformer.py     # TokenTransformer class — format conversion
  SKILL.md
  requirements.txt   # mcp, httpx, pydantic>=2

tests/services/skills/design-token-transform/
  test_figma_client.py
  test_transformer.py
  conftest.py
```

---

## Phase 0 — Scaffolding

### Task 0.1 — Create directory structure

- [ ] Create the skill server and test directories.

```bash
mkdir -p /Users/zachstallbohm/Work/gemma/services/skills/design-token-transform
mkdir -p /Users/zachstallbohm/Work/gemma/tests/services/skills/design-token-transform
```

### Task 0.2 — Write requirements.txt

- [ ] Create `services/skills/design-token-transform/requirements.txt`:

```text
mcp>=1.0.0
httpx>=0.27.0
pydantic>=2.0.0
```

### Task 0.3 — Write SKILL.md

- [ ] Create `services/skills/design-token-transform/SKILL.md` (frontmatter exactly as below; body is model-agnostic markdown, no absolute paths):

```markdown
---
name: design-token-transform
description: >
  Extracts design tokens (colors, typography, spacing, radii) from Figma via REST
  API and transforms them into CSS variables, Tailwind config, or shadcn/ui globals.
  Use when generating components that should match a Figma design system — makes
  screenshot-to-component output token-aware instead of using hardcoded values.
  Requires FIGMA_ACCESS_TOKEN env var.
trigger: "Use when syncing design tokens from Figma or converting a design system to code"
tools:
  - design_token.extract
  - design_token.transform
  - design_token.extract_and_transform
version: "0.1.0"
license: MIT
requires: []
---

# Design Token Transform Skill

You have access to the `design_token` MCP server. It pulls design tokens from a
Figma file via the Figma REST API and converts them into front-end formats so
generated components match an existing design system instead of using guessed,
hardcoded values.

## When to Use

- Generating a component that must match a Figma design system
- Syncing colors / typography / spacing / radii from Figma into code
- Producing a `tailwind.config.js` theme, CSS custom properties, or shadcn/ui
  HSL variables from a Figma file

## Setup

The server requires the `FIGMA_ACCESS_TOKEN` environment variable (a Figma
personal access token). Without it, every tool returns an error.

## Available Tools

### `design_token.extract`

Fetch raw tokens from a Figma file. Returns JSON: a `TokenSet`.

```json
{ "figma_file_key": "abc123", "node_id": "1:2" }
```

`node_id` is optional; omit it to scan the whole document.

### `design_token.transform`

Convert a raw `TokenSet` JSON string into a target format.

```json
{ "tokens_json": "{...}", "format": "tailwind" }
```

`format` is one of `tailwind` | `css-vars` | `shadcn`.

### `design_token.extract_and_transform`

Extract then transform in one call. Optionally write the result to a file.

```json
{ "figma_file_key": "abc123", "format": "css-vars", "output_path": "/tmp/tokens.css" }
```

## Output Formats

- `tailwind`: a `tailwind.config.js` theme-extension object string.
- `css-vars`: CSS custom properties under `:root { ... }`.
- `shadcn`: shadcn/ui-style HSL variables for `globals.css`.
```

---

## Phase 1 — Shared types

### Task 1.1 — Define pydantic models in figma_client.py

- [ ] Create `services/skills/design-token-transform/figma_client.py` with the logging guard and the data models. Everything below is appended to this same file in later tasks.

```python
import logging
import os
import sys
from datetime import datetime, timezone

import httpx
from pydantic import BaseModel

# CRITICAL: stderr only. Configure before anything that may log.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("design-token-transform.figma")


class DesignToken(BaseModel):
    name: str
    category: str  # 'color' | 'typography' | 'spacing' | 'radius' | 'shadow'
    value: str     # raw value e.g. "#FF5733" or "16px" or "400"
    description: str = ""


class TokenSet(BaseModel):
    source: str  # figma file key
    tokens: list[DesignToken]
    extracted_at: str
```

---

## Phase 2 — FigmaClient

### Task 2.1 — Implement the constructor with env validation

- [ ] Append the `FigmaClient.__init__` to `figma_client.py`. It reads the token and base URL from the environment and fails loudly if the token is missing.

```python
class FigmaClient:
    def __init__(self) -> None:
        self._token = os.getenv("FIGMA_ACCESS_TOKEN")
        if not self._token:
            raise RuntimeError(
                "FIGMA_ACCESS_TOKEN is not set. Export a Figma personal access "
                "token before using the design-token-transform skill."
            )
        self._base = os.getenv("FIGMA_API_BASE", "https://api.figma.com/v1")
        log.info("FigmaClient configured, base=%s", self._base)
```

### Task 2.2 — Implement get_file_tokens (the HTTP call)

- [ ] Append `get_file_tokens`. It calls `GET /files/:key` (or `/files/:key/nodes?ids=` when a node_id is given), sends the token header, raises on non-2xx, and delegates tree traversal to `_extract_tokens_from_node`.

```python
    async def get_file_tokens(
        self, file_key: str, node_id: str | None = None
    ) -> TokenSet:
        headers = {"X-Figma-Token": self._token}
        async with httpx.AsyncClient(timeout=30.0) as client:
            if node_id:
                url = f"{self._base}/files/{file_key}/nodes"
                resp = await client.get(url, headers=headers, params={"ids": node_id})
            else:
                url = f"{self._base}/files/{file_key}"
                resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        # Whole-file responses put the tree under "document"; node responses
        # put each requested node under "nodes"[id]["document"].
        if node_id:
            roots = [
                entry["document"]
                for entry in data.get("nodes", {}).values()
                if entry and "document" in entry
            ]
        else:
            roots = [data["document"]] if "document" in data else []

        tokens: list[DesignToken] = []
        for root in roots:
            tokens.extend(self._extract_tokens_from_node(root))

        # Deduplicate by (category, name), keep first; deterministic order.
        seen: set[tuple[str, str]] = set()
        unique: list[DesignToken] = []
        for tok in sorted(tokens, key=lambda t: (t.category, t.name)):
            key = (tok.category, tok.name)
            if key not in seen:
                seen.add(key)
                unique.append(tok)

        log.info("extracted %d unique tokens from %s", len(unique), file_key)
        return TokenSet(
            source=file_key,
            tokens=unique,
            extracted_at=datetime.now(timezone.utc).isoformat(),
        )
```

### Task 2.3 — Implement _extract_tokens_from_node (tree traversal)

- [ ] Append `_extract_tokens_from_node`. It walks the Figma node tree recursively and emits a DesignToken per recognized property. Colors come from solid fills, typography from text `style`, radii from `cornerRadius`, spacing from auto-layout `itemSpacing`/padding. Node `name` becomes the token name.

```python
    def _extract_tokens_from_node(self, node: dict) -> list[DesignToken]:
        tokens: list[DesignToken] = []
        name = node.get("name", "").strip() or node.get("id", "unnamed")

        # --- Color: first solid fill ---
        for fill in node.get("fills", []) or []:
            if fill.get("type") == "SOLID" and fill.get("visible", True):
                color = fill.get("color", {})
                hex_value = self._rgba_to_hex(color, fill.get("opacity"))
                tokens.append(
                    DesignToken(
                        name=name,
                        category="color",
                        value=hex_value,
                        description=f"fill from node {node.get('id', '')}",
                    )
                )
                break

        # --- Typography: text style block ---
        style = node.get("style")
        if isinstance(style, dict) and "fontSize" in style:
            size = style["fontSize"]
            tokens.append(
                DesignToken(
                    name=name, category="typography",
                    value=f"{self._num(size)}px",
                    description=f"fontFamily={style.get('fontFamily', '')} "
                                f"weight={style.get('fontWeight', '')}",
                )
            )

        # --- Radius ---
        radius = node.get("cornerRadius")
        if isinstance(radius, (int, float)):
            tokens.append(
                DesignToken(name=name, category="radius",
                            value=f"{self._num(radius)}px")
            )

        # --- Spacing: auto-layout item spacing ---
        spacing = node.get("itemSpacing")
        if isinstance(spacing, (int, float)):
            tokens.append(
                DesignToken(name=name, category="spacing",
                            value=f"{self._num(spacing)}px")
            )

        # Recurse into children.
        for child in node.get("children", []) or []:
            tokens.extend(self._extract_tokens_from_node(child))

        return tokens

    @staticmethod
    def _rgba_to_hex(color: dict, opacity: float | None) -> str:
        r = round(color.get("r", 0) * 255)
        g = round(color.get("g", 0) * 255)
        b = round(color.get("b", 0) * 255)
        a = color.get("a", 1) if opacity is None else opacity
        if a is not None and a < 1:
            return f"#{r:02X}{g:02X}{b:02X}{round(a * 255):02X}"
        return f"#{r:02X}{g:02X}{b:02X}"

    @staticmethod
    def _num(value: float) -> str:
        # Drop trailing .0 so 16.0 -> "16" but 1.5 -> "1.5". Keeps output stable.
        return str(int(value)) if float(value).is_integer() else str(value)
```

---

## Phase 3 — TokenTransformer

### Task 3.1 — Create transformer.py with the dispatcher

- [ ] Create `services/skills/design-token-transform/transformer.py`. It imports the types from `figma_client` and routes `transform` to per-format methods. Unknown formats raise `ValueError`.

```python
import logging
import re
import sys

from figma_client import DesignToken, TokenSet

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("design-token-transform.transformer")


class TokenTransformer:
    def transform(self, tokens: TokenSet, format: str) -> str:
        if format == "tailwind":
            return self._to_tailwind(tokens)
        if format == "css-vars":
            return self._to_css_vars(tokens)
        if format == "shadcn":
            return self._to_shadcn(tokens)
        raise ValueError(
            f"unknown format {format!r}; expected 'tailwind', 'css-vars', or 'shadcn'"
        )

    @staticmethod
    def _slug(name: str) -> str:
        # "Primary / Blue 500" -> "primary-blue-500"
        s = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower())
        return s.strip("-") or "token"

    @staticmethod
    def _by_category(tokens: TokenSet, category: str) -> list[DesignToken]:
        return [t for t in tokens.tokens if t.category == category]
```

### Task 3.2 — Implement _to_css_vars

- [ ] Append `_to_css_vars`. Emits `:root { --<category>-<slug>: <value>; }` with one variable per token, grouped by category, deterministic order.

```python
    def _to_css_vars(self, tokens: TokenSet) -> str:
        lines = [":root {"]
        prefix = {
            "color": "color",
            "typography": "font-size",
            "spacing": "spacing",
            "radius": "radius",
            "shadow": "shadow",
        }
        for category in ["color", "typography", "spacing", "radius", "shadow"]:
            items = self._by_category(tokens, category)
            if not items:
                continue
            lines.append(f"  /* {category} */")
            for tok in items:
                var = f"--{prefix[category]}-{self._slug(tok.name)}"
                lines.append(f"  {var}: {tok.value};")
        lines.append("}")
        return "\n".join(lines)
```

### Task 3.3 — Implement _to_tailwind

- [ ] Append `_to_tailwind`. Builds a `module.exports = { theme: { extend: { ... } } }` string mapping Tailwind keys (`colors`, `fontSize`, `spacing`, `borderRadius`) to slug→value objects.

```python
    def _to_tailwind(self, tokens: TokenSet) -> str:
        groups = {
            "colors": self._by_category(tokens, "color"),
            "fontSize": self._by_category(tokens, "typography"),
            "spacing": self._by_category(tokens, "spacing"),
            "borderRadius": self._by_category(tokens, "radius"),
        }
        sections: list[str] = []
        for key, items in groups.items():
            if not items:
                continue
            entries = ",\n".join(
                f'        "{self._slug(t.name)}": "{t.value}"' for t in items
            )
            sections.append(f"      {key}: {{\n{entries}\n      }}")
        body = ",\n".join(sections)
        return (
            "/** @type {import('tailwindcss').Config} */\n"
            "module.exports = {\n"
            "  theme: {\n"
            "    extend: {\n"
            f"{body}\n"
            "    }\n"
            "  }\n"
            "}"
        )
```

### Task 3.4 — Implement _to_shadcn (HSL variables)

- [ ] Append `_to_shadcn`. Colors are converted to shadcn's space-separated HSL channel format (`H S% L%`). Non-color tokens that shadcn expects (`--radius`) are emitted as-is; the rest are skipped because shadcn's contract is color + radius centric.

```python
    def _to_shadcn(self, tokens: TokenSet) -> str:
        lines = ["@layer base {", "  :root {"]
        for tok in self._by_category(tokens, "color"):
            h, s, l = self._hex_to_hsl(tok.value)
            lines.append(f"    --{self._slug(tok.name)}: {h} {s}% {l}%;")
        radii = self._by_category(tokens, "radius")
        if radii:
            # shadcn uses a single --radius; take the first deterministic one.
            lines.append(f"    --radius: {radii[0].value};")
        lines.append("  }")
        lines.append("}")
        return "\n".join(lines)

    @staticmethod
    def _hex_to_hsl(hex_value: str) -> tuple[int, int, int]:
        h = hex_value.lstrip("#")
        r = int(h[0:2], 16) / 255
        g = int(h[2:4], 16) / 255
        b = int(h[4:6], 16) / 255
        mx, mn = max(r, g, b), min(r, g, b)
        l = (mx + mn) / 2
        if mx == mn:
            hue = sat = 0.0
        else:
            d = mx - mn
            sat = d / (2 - mx - mn) if l > 0.5 else d / (mx + mn)
            if mx == r:
                hue = (g - b) / d + (6 if g < b else 0)
            elif mx == g:
                hue = (b - r) / d + 2
            else:
                hue = (r - g) / d + 4
            hue /= 6
        return round(hue * 360), round(sat * 100), round(l * 100)
```

---

## Phase 4 — MCP server

### Task 4.1 — Create server.py with imports, logging, and the app

- [ ] Create `services/skills/design-token-transform/server.py`. Configure stderr logging BEFORE importing the local modules.

```python
import asyncio
import json
import logging
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# CRITICAL: stderr only. Configure before importing local modules that log.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("design-token-transform.server")

from figma_client import FigmaClient, TokenSet  # noqa: E402
from transformer import TokenTransformer        # noqa: E402

app: Server = Server("design-token-transform")
transformer = TokenTransformer()
```

### Task 4.2 — Implement list_tools

- [ ] Append the `list_tools` handler exposing all three tools with self-contained JSON Schemas. Note the dotted tool names (`design_token.extract`).

```python
@app.list_tools()
async def list_tools() -> list[Tool]:
    format_prop = {
        "type": "string",
        "enum": ["tailwind", "css-vars", "shadcn"],
        "default": "tailwind",
        "description": "Target format for the transformed tokens.",
    }
    return [
        Tool(
            name="design_token.extract",
            description=(
                "Fetch design tokens from a Figma file via the REST API. "
                "Returns a TokenSet as JSON."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "figma_file_key": {
                        "type": "string",
                        "description": "Figma file key (from the file URL).",
                    },
                    "node_id": {
                        "type": "string",
                        "description": "Optional node id to scope extraction.",
                    },
                },
                "required": ["figma_file_key"],
            },
        ),
        Tool(
            name="design_token.transform",
            description=(
                "Transform a raw TokenSet JSON string into a target format "
                "(tailwind | css-vars | shadcn). Returns the format string."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "tokens_json": {
                        "type": "string",
                        "description": "A TokenSet serialized as JSON.",
                    },
                    "format": format_prop,
                },
                "required": ["tokens_json"],
            },
        ),
        Tool(
            name="design_token.extract_and_transform",
            description=(
                "Extract tokens from a Figma file and transform them in one call. "
                "Optionally writes the result to output_path. Returns the format string."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "figma_file_key": {"type": "string"},
                    "format": format_prop,
                    "output_path": {
                        "type": "string",
                        "description": "Optional path to write the transformed output.",
                    },
                },
                "required": ["figma_file_key"],
            },
        ),
    ]
```

### Task 4.3 — Implement call_tool

- [ ] Append the `call_tool` dispatcher. It lazily constructs the FigmaClient (so a missing token only errors on use, not at import), routes by tool name, and returns errors as content rather than crashing the stdio server.

```python
_client: FigmaClient | None = None


def _get_client() -> FigmaClient:
    global _client
    if _client is None:
        _client = FigmaClient()  # raises if FIGMA_ACCESS_TOKEN missing
    return _client


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "design_token.extract":
            token_set = await _get_client().get_file_tokens(
                arguments["figma_file_key"], arguments.get("node_id")
            )
            text = token_set.model_dump_json()

        elif name == "design_token.transform":
            token_set = TokenSet.model_validate_json(arguments["tokens_json"])
            text = transformer.transform(
                token_set, arguments.get("format", "tailwind")
            )

        elif name == "design_token.extract_and_transform":
            token_set = await _get_client().get_file_tokens(
                arguments["figma_file_key"]
            )
            text = transformer.transform(
                token_set, arguments.get("format", "tailwind")
            )
            out_path = arguments.get("output_path")
            if out_path:
                with open(out_path, "w", encoding="utf-8") as fh:
                    fh.write(text)
                log.info("wrote transformed tokens to %s", out_path)

        else:
            log.error("unknown tool: %s", name)
            return [TextContent(type="text", text=f"unknown tool: {name}")]

    except Exception as exc:  # never let one bad call crash the stdio server
        log.exception("tool %s failed", name)
        return [TextContent(type="text", text=f"error: {exc!r}")]

    return [TextContent(type="text", text=text)]
```

### Task 4.4 — Implement main and entry guard

- [ ] Append the async `main` and the `__main__` guard:

```python
async def main() -> None:
    log.info("starting design-token-transform MCP server")
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Phase 5 — Tests (mocked, no real Figma API)

### Task 5.1 — Create conftest.py with sys.path, fixtures, and a fake Figma response

- [ ] Create `tests/services/skills/design-token-transform/conftest.py`. It puts the server dir on `sys.path` and provides a realistic Figma node-tree fixture plus a ready-made TokenSet.

```python
import sys
from pathlib import Path

import pytest

SERVER_DIR = (
    Path(__file__).resolve().parents[4]
    / "services" / "skills" / "design-token-transform"
)
sys.path.insert(0, str(SERVER_DIR))


@pytest.fixture
def figma_file_response() -> dict:
    """A minimal but realistic GET /files/:key response."""
    return {
        "document": {
            "id": "0:0",
            "name": "Document",
            "children": [
                {
                    "id": "1:1",
                    "name": "Primary",
                    "fills": [
                        {"type": "SOLID", "visible": True,
                         "color": {"r": 1.0, "g": 0.341, "b": 0.2, "a": 1.0}}
                    ],
                },
                {
                    "id": "1:2",
                    "name": "Heading",
                    "style": {"fontSize": 16, "fontFamily": "Inter", "fontWeight": 700},
                },
                {
                    "id": "1:3",
                    "name": "Card",
                    "cornerRadius": 8,
                    "itemSpacing": 12,
                },
            ],
        }
    }


@pytest.fixture
def sample_token_set():
    from figma_client import DesignToken, TokenSet
    return TokenSet(
        source="abc123",
        extracted_at="2026-06-17T00:00:00+00:00",
        tokens=[
            DesignToken(name="Primary", category="color", value="#FF5733"),
            DesignToken(name="Heading", category="typography", value="16px"),
            DesignToken(name="Card", category="radius", value="8px"),
            DesignToken(name="Card", category="spacing", value="12px"),
        ],
    )
```

### Task 5.2 — Test: missing FIGMA_ACCESS_TOKEN raises a clear error

- [ ] Create `tests/services/skills/design-token-transform/test_figma_client.py` starting with the env-validation test. Use `monkeypatch.delenv` so the test is hermetic.

```python
import pytest

pytestmark = [pytest.mark.mocked, pytest.mark.asyncio]


def test_missing_token_raises(monkeypatch):
    monkeypatch.delenv("FIGMA_ACCESS_TOKEN", raising=False)
    from figma_client import FigmaClient
    with pytest.raises(RuntimeError, match="FIGMA_ACCESS_TOKEN"):
        FigmaClient()
```

### Task 5.3 — Test: get_file_tokens parses a mocked Figma response

- [ ] Append a test that patches `httpx.AsyncClient` so no real network call happens, and asserts the extracted DesignToken objects.

```python
class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, params=None):
        return _FakeResponse(self._payload)


async def test_get_file_tokens_extracts(monkeypatch, figma_file_response):
    monkeypatch.setenv("FIGMA_ACCESS_TOKEN", "tok")
    import figma_client as fc
    monkeypatch.setattr(
        fc.httpx, "AsyncClient",
        lambda *a, **k: _FakeAsyncClient(figma_file_response),
    )

    client = fc.FigmaClient()
    token_set = await client.get_file_tokens("abc123")

    by_cat = {(t.category, t.name): t.value for t in token_set.tokens}
    assert token_set.source == "abc123"
    assert by_cat[("color", "Primary")] == "#FF5733"
    assert by_cat[("typography", "Heading")] == "16px"
    assert by_cat[("radius", "Card")] == "8px"
    assert by_cat[("spacing", "Card")] == "12px"
```

### Task 5.4 — Test: rgba_to_hex and node traversal edge cases

- [ ] Append tests for the color conversion helper (with and without alpha) and for a node with no extractable properties yielding no tokens.

```python
def test_rgba_to_hex_opaque(monkeypatch):
    monkeypatch.setenv("FIGMA_ACCESS_TOKEN", "tok")
    from figma_client import FigmaClient
    assert FigmaClient._rgba_to_hex({"r": 1.0, "g": 0.341, "b": 0.2}, None) == "#FF5733"


def test_rgba_to_hex_with_alpha(monkeypatch):
    monkeypatch.setenv("FIGMA_ACCESS_TOKEN", "tok")
    from figma_client import FigmaClient
    out = FigmaClient._rgba_to_hex({"r": 0, "g": 0, "b": 0}, 0.5)
    assert out == "#00000080"


def test_empty_node_yields_no_tokens(monkeypatch):
    monkeypatch.setenv("FIGMA_ACCESS_TOKEN", "tok")
    from figma_client import FigmaClient
    client = FigmaClient()
    assert client._extract_tokens_from_node({"id": "x", "name": "Frame"}) == []
```

### Task 5.5 — Test: _to_css_vars output shape

- [ ] Create `tests/services/skills/design-token-transform/test_transformer.py` with the css-vars test.

```python
import pytest

pytestmark = pytest.mark.mocked


def test_to_css_vars(sample_token_set):
    from transformer import TokenTransformer
    out = TokenTransformer().transform(sample_token_set, "css-vars")
    assert out.startswith(":root {")
    assert out.rstrip().endswith("}")
    assert "--color-primary: #FF5733;" in out
    assert "--font-size-heading: 16px;" in out
    assert "--radius-card: 8px;" in out
```

### Task 5.6 — Test: _to_tailwind output shape

- [ ] Append the tailwind test asserting the `theme.extend` structure and slugged keys.

```python
def test_to_tailwind(sample_token_set):
    from transformer import TokenTransformer
    out = TokenTransformer().transform(sample_token_set, "tailwind")
    assert "module.exports" in out
    assert "theme: {" in out
    assert "extend: {" in out
    assert "colors: {" in out
    assert '"primary": "#FF5733"' in out
    assert "fontSize: {" in out
    assert '"heading": "16px"' in out
    assert "borderRadius: {" in out
```

### Task 5.7 — Test: _to_shadcn produces HSL variables

- [ ] Append the shadcn test verifying HSL channel format and `--radius`.

```python
def test_to_shadcn(sample_token_set):
    from transformer import TokenTransformer
    out = TokenTransformer().transform(sample_token_set, "shadcn")
    assert "@layer base {" in out
    assert ":root {" in out
    # #FF5733 -> H S% L%
    assert "--primary: 11 100% 56%;" in out
    assert "--radius: 8px;" in out


def test_hex_to_hsl_known_value():
    from transformer import TokenTransformer
    assert TokenTransformer._hex_to_hsl("#FF5733") == (11, 100, 56)


def test_unknown_format_raises(sample_token_set):
    from transformer import TokenTransformer
    with pytest.raises(ValueError, match="unknown format"):
        TokenTransformer().transform(sample_token_set, "scss")
```

### Task 5.8 — Test: extract_and_transform combines both steps

- [ ] Append an end-to-end test against the server's `call_tool`, mocking the Figma HTTP call, asserting the transformed output and that `output_path` is written.

```python
async def test_extract_and_transform(monkeypatch, tmp_path, figma_file_response):
    monkeypatch.setenv("FIGMA_ACCESS_TOKEN", "tok")
    import figma_client as fc
    monkeypatch.setattr(
        fc.httpx, "AsyncClient",
        lambda *a, **k: _FakeAsyncClient(figma_file_response),
    )

    # Import server after env + patch so its lazy client picks them up.
    import server
    server._client = None  # reset any cached client

    out_file = tmp_path / "tokens.css"
    result = await server.call_tool(
        "design_token.extract_and_transform",
        {"figma_file_key": "abc123", "format": "css-vars",
         "output_path": str(out_file)},
    )
    text = result[0].text
    assert "--color-primary: #FF5733;" in text
    assert out_file.read_text() == text
```

> This test reuses `_FakeAsyncClient` / `_FakeResponse` from `test_figma_client.py`. If pytest collection does not share them, move those two helper classes into `conftest.py` and import them in both test modules. Add the import line at the top of `test_transformer.py`:
> `from test_figma_client import _FakeAsyncClient, _FakeResponse` (or from conftest).

### Task 5.9 — Configure pytest-asyncio and run the suite

- [ ] Ensure `pytest-asyncio` is available (dev dependency) and the repo's pytest config registers the `mocked` and `live` markers (per `research/llm-harness-research/specs/spec_testing.md`). If a local config is needed, add `asyncio_mode = "auto"` so the `async def test_*` functions run without per-test decorators.
- [ ] Run the suite and confirm green:

```bash
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skills/design-token-transform/ -v -m mocked
```

---

## Phase 6 — Verification

### Task 6.1 — Smoke-test the server over stdio

- [ ] Verify the server starts and lists its three tools without leaking to stdout. With `FIGMA_ACCESS_TOKEN` set, run a tiny stdio client that calls `list_tools()` and asserts the three dotted tool names are present.

```python
import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    params = StdioServerParameters(command="python", args=["server.py"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = await s.list_tools()
            names = {t.name for t in tools.tools}
            assert names == {
                "design_token.extract",
                "design_token.transform",
                "design_token.extract_and_transform",
            }, names
            print("OK", names, file=__import__("sys").stderr)


asyncio.run(main())
```

Run from the server directory:

```bash
cd /Users/zachstallbohm/Work/gemma/services/skills/design-token-transform
FIGMA_ACCESS_TOKEN=dummy python smoke_test.py
```

(`dummy` is fine here — `list_tools` does not call Figma. Remove `smoke_test.py` afterward.)

### Task 6.2 — Confirm critical rules hold

- [ ] Grep the server tree for stdout violations — there must be zero `print(` calls:

```bash
grep -rn "print(" /Users/zachstallbohm/Work/gemma/services/skills/design-token-transform/*.py || echo "clean"
```

- [ ] Confirm no hardcoded design values: every color/size in `transformer.py` is derived from the token, not literal (the only literals are format scaffolding like `:root {`, `module.exports`, `@layer base`).
- [ ] Confirm `httpx` is used (not `requests`) and the token is sent via the `X-Figma-Token` header.

---

## Done criteria

- [ ] All files created at the paths in the file-structure section.
- [ ] `python -m pytest tests/services/skills/design-token-transform/ -m mocked` is green.
- [ ] `list_tools` exposes exactly the three dotted tools.
- [ ] Missing `FIGMA_ACCESS_TOKEN` raises a clear `RuntimeError`.
- [ ] No `print()` anywhere; all logging to stderr.
- [ ] Transforms are deterministic and contain no hardcoded design values.
