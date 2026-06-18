# screenshot-to-component MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the screenshot-to-component Python MCP server implementing ScreenCoder's 3-stage modular pipeline (grounding → planning → generation) for converting UI screenshots into React + Tailwind components.

**Architecture:** UIGrounder encodes the image as base64 and sends it to Gemma 4 vision via litellm, extracting bounding boxes and semantic labels into a UIElement tree. LayoutPlanner takes the GroundingResult and reasons about hierarchy/layout/color. ComponentGenerator synthesizes React + Tailwind code from the LayoutPlan. All three stages are independently callable via MCP tools and individually testable. All LLM calls hit GEMMA_BASE (single-GPU mode).

**Tech Stack:** Python 3.11+, `mcp` SDK, `litellm`, `pydantic>=2`, `Pillow`, `pytest`

---

## Why modular (research grounding)

From `docs/frontend-design-skills-research-2026-06-17.md`: ScreenCoder (arXiv:2507.22827) shows a modular **grounding → planning → generation** pipeline beats a monolithic image-in/code-out VLM (0.755 vs 0.730 block match). We therefore decompose the problem into three independently-callable, independently-testable stages rather than one prompt. abi/screenshot-to-code (72k stars) is the reference implementation for the overall capability.

- **Grounding** — VLM detects bounding boxes + semantic labels (header, nav, sidebar, card, button, …).
- **Planning** — derive a hierarchical layout plan from the grounding output.
- **Generation** — synthesize React + Tailwind from the structured plan.

---

## Critical constraints (apply to every task)

- **stdout is sacred.** All logging uses `logging` configured with `stream=sys.stderr`. NEVER `print()`. stdout carries JSON-RPC 2.0 framing; any stray byte corrupts the stream silently and produces misleading `Parse error` symptoms downstream. litellm is chatty — its loggers MUST be on stderr (the root `basicConfig(stream=sys.stderr)` in `server.py` covers this).
- **Single-GPU.** EVERY LLM call (vision grounding, text planning, code generation) targets `GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")`. There is NO QWEN_BASE — do not introduce one. Model name comes from `GEMMA_MODEL = os.getenv("GEMMA_MODEL", "openai/google/gemma-4-31B-it")` (litellm uses the `openai/` prefix to route to an OpenAI-compatible server).
- **Never tiktoken.** If token counting is ever needed, use `AutoTokenizer.from_pretrained("google/gemma-4-9b-it")`. Gemma uses SentencePiece; tiktoken counts are wrong. (This skill does not count tokens today; the rule still applies if added.)
- **Images go to Gemma 4 as base64-encoded PNG** in the vision message content (`{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}`).
- **`generate()` chains grounding → planning → generation in sequence.** Each stage is a separate method on a separate class and is independently testable.
- **Output target is React + Tailwind by default.** `framework` ∈ {`react-tailwind`, `html-css`, `vue-tailwind`}.
- The server is a **child process** spawned by the SkillRegistry over stdio. It must not assume a TTY, must not print banners, and writes generated code only to the caller-supplied `output_path`.

---

## Phase 0 — Scaffolding

### Task 0.1 — Create directory structure

- [ ] Create the skill server and test directories.

```bash
mkdir -p /Users/zachstallbohm/Work/gemma/services/skills/screenshot-to-component
mkdir -p /Users/zachstallbohm/Work/gemma/tests/services/skills/screenshot-to-component
```

### Task 0.2 — Write requirements.txt

- [ ] Create `services/skills/screenshot-to-component/requirements.txt`:

```text
mcp>=1.0.0
litellm>=1.40.0
pydantic>=2.0
Pillow>=10.0.0
```

### Task 0.3 — Write SKILL.md

- [ ] Create `services/skills/screenshot-to-component/SKILL.md` (frontmatter EXACTLY as below; body is model-agnostic markdown, no absolute paths):

```markdown
---
name: screenshot-to-component
description: >
  Converts a UI screenshot or mockup into a React + Tailwind component using a
  3-stage modular pipeline: Gemma 4 vision grounding (detect UI elements and layout),
  layout planning (hierarchical structure), and code generation. Produces shadcn/ui-
  compatible Tailwind components. Chain with react-doctor for immediate quality checking.
trigger: "Use when converting a UI screenshot, mockup, or design image into a React component"
tools:
  - screenshot_to_component.generate
  - screenshot_to_component.ground
  - screenshot_to_component.plan
version: "0.1.0"
license: MIT
requires: [react-doctor]
---

# Screenshot to Component Skill

You have access to the `screenshot_to_component` MCP server, which turns a UI
screenshot or mockup image into a React + Tailwind component. It uses a 3-stage
modular pipeline (grounding → planning → generation) rather than a single
image-in/code-out prompt, which produces more faithful layouts.

## When to Use

- Converting a screenshot, mockup, or design image into a React component
- Inspecting just the detected UI elements (grounding) or the layout plan before
  committing to full code generation

## Available Tools

### `screenshot_to_component.generate`

Run the full pipeline. Returns JSON: `component_code`, `layout_plan`, `output_path`.

```json
{ "image_path": "/mocks/dashboard.png", "framework": "react-tailwind", "output_path": "/out/Dashboard.tsx" }
```

### `screenshot_to_component.ground`

Grounding stage only. Returns JSON with bounding boxes + semantic labels. Useful
for inspecting what the vision model detected.

```json
{ "image_path": "/mocks/dashboard.png" }
```

### `screenshot_to_component.plan`

Planning stage only. Takes a grounding JSON string, returns a hierarchical layout plan.

```json
{ "grounding_result": "{\"elements\": [...], \"image_width\": 1440, \"image_height\": 900}" }
```

## Frameworks

- `react-tailwind` (default): shadcn/ui-compatible React + Tailwind component.
- `html-css`: a single HTML file with plain CSS.
- `vue-tailwind`: a Vue single-file component using Tailwind.

## Chaining

After generation, run `react-doctor` on the produced component to catch
accessibility, type, and lint issues immediately.

## Limitations

- Bounding boxes and colors are inferred by the vision model and are approximate.
- Pixel-perfect reproduction is not a goal; the output is a faithful, editable scaffold.
```

---

## Phase 1 — Shared models and config

### Task 1.1 — Create models.py with the pydantic types

- [ ] Create `services/skills/screenshot-to-component/models.py`. These types are shared across all three stages and define the JSON contract returned over MCP:

```python
"""Shared pydantic models for the screenshot_to_component skill.

CRITICAL: this module is loaded inside an MCP stdio child process.
NEVER print() or write to stdout. All logging goes to sys.stderr.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class BoundingBox(BaseModel):
    x: float
    y: float
    width: float
    height: float


class UIElement(BaseModel):
    # 'header' | 'nav' | 'sidebar' | 'card' | 'button' | 'input' | 'text' | 'image' | 'other'
    label: str
    bounds: BoundingBox
    children: list["UIElement"] = Field(default_factory=list)
    description: str = ""  # e.g. "primary CTA button with blue background"


class GroundingResult(BaseModel):
    elements: list[UIElement] = Field(default_factory=list)
    image_width: int
    image_height: int


class LayoutPlan(BaseModel):
    root_component: str = "flex flex-col min-h-screen"  # root Tailwind container classes
    sections: list[dict] = Field(default_factory=list)  # hierarchical section descriptions
    color_palette: list[str] = Field(default_factory=list)  # inferred hex colors
    typography_notes: str = ""


class GenerationResult(BaseModel):
    component_code: str
    framework: str
    output_path: str | None = None


# UIElement references itself in `children`; rebuild to resolve the forward ref.
UIElement.model_rebuild()
```

### Task 1.2 — Create llm.py: shared litellm config + JSON extraction helper

- [ ] Create `services/skills/screenshot-to-component/llm.py`. This centralizes the single-GPU GEMMA_BASE config, the litellm call wrapper, the base64 image encoder, and a tolerant JSON extractor (LLMs wrap JSON in prose / code fences). Every stage imports from here so the single-GPU rule lives in exactly one place:

```python
"""Shared LLM access for the screenshot_to_component skill.

SINGLE-GPU: every call targets GEMMA_BASE. There is no QWEN_BASE.
stdout is sacred — never print(); litellm is routed to stderr via root logging.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
from pathlib import Path

import litellm

log = logging.getLogger("screenshot-to-component.llm")

# SINGLE-GPU: all LLM calls (vision + text) hit the one Gemma 4 server.
GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")
GEMMA_MODEL = os.getenv("GEMMA_MODEL", "openai/google/gemma-4-31B-it")
GEMMA_API_KEY = os.getenv("GEMMA_API_KEY", "not-needed")  # vLLM ignores the key


def encode_image_b64(image_path: str) -> tuple[str, int, int]:
    """Read an image, return (base64 PNG data URL payload, width, height).

    Always re-encodes to PNG so the data URL mime type is correct regardless of
    the source format (jpg, webp, etc.).
    """
    from io import BytesIO

    from PIL import Image

    src = Path(image_path)
    if not src.is_file():
        raise FileNotFoundError(f"image not found: {image_path}")

    with Image.open(src) as img:
        img = img.convert("RGB")
        width, height = img.size
        buf = BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return b64, width, height


def call_llm(messages: list[dict], *, temperature: float = 0.2, max_tokens: int = 4096) -> str:
    """Single litellm chat completion against the Gemma 4 server. Returns content text."""
    log.info("LLM call: %d message(s), max_tokens=%d", len(messages), max_tokens)
    resp = litellm.completion(
        model=GEMMA_MODEL,
        api_base=GEMMA_BASE,
        api_key=GEMMA_API_KEY,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return resp["choices"][0]["message"]["content"] or ""


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of an LLM response, tolerating code fences/prose."""
    # Strip ```json ... ``` fences if present.
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        # Fall back to the first balanced-looking { ... } span.
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = text[start : end + 1]
    if candidate is None:
        raise ValueError(f"no JSON object found in LLM response: {text[:200]!r}")
    return json.loads(candidate)
```

---

## Phase 2 — UIGrounder (stage 1: vision)

### Task 2.1 — Create grounder.py with the prompt and class shell

- [ ] Create `services/skills/screenshot-to-component/grounder.py`. The system prompt asks Gemma 4 to return a strict JSON schema of UI elements. Coordinates are requested in absolute pixels relative to the supplied image dimensions:

```python
"""UIGrounder: Gemma 4 vision -> bounding boxes + semantic labels.

stdout is sacred — never print(). SINGLE-GPU — all calls via llm.call_llm.
"""
from __future__ import annotations

import logging

from llm import call_llm, encode_image_b64, extract_json
from models import GroundingResult

log = logging.getLogger("screenshot-to-component.grounder")

GROUNDING_SYSTEM = (
    "You are a UI grounding model. Given a screenshot, detect the visible UI "
    "elements and return STRICT JSON only (no prose, no code fences).\n"
    "Schema:\n"
    '{ "elements": [ { "label": <one of: header|nav|sidebar|card|button|input|'
    'text|image|other>, "bounds": {"x": <px>, "y": <px>, "width": <px>, '
    '"height": <px>}, "description": <short string>, "children": [ ...same '
    'shape... ] } ] }\n'
    "Coordinates are absolute pixels with origin at the top-left of the image. "
    "Nest elements as children when one visually contains another (e.g. buttons "
    "inside a card). Return every salient region."
)


class UIGrounder:
    """Stage 1: detect UI elements + bounding boxes from a screenshot."""

    def __init__(self, model: str | None = None) -> None:
        self._model = model  # reserved; llm module resolves the default model
```

### Task 2.2 — Implement UIGrounder.ground

- [ ] Append `ground`. It encodes the image as base64 FIRST (this ordering is asserted in tests), builds a vision message, calls the LLM, parses JSON, and stamps the real image dimensions onto the result (the model's own width/height guesses are discarded in favor of the measured values):

```python
    def ground(self, image_path: str) -> GroundingResult:
        b64, width, height = encode_image_b64(image_path)  # base64 BEFORE the LLM call
        log.info("grounding image %s (%dx%d)", image_path, width, height)

        messages = [
            {"role": "system", "content": GROUNDING_SYSTEM},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Image is {width}x{height} pixels. Detect the UI "
                            "elements and return the JSON described above."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    },
                ],
            },
        ]

        raw = call_llm(messages, temperature=0.1, max_tokens=4096)
        data = extract_json(raw)
        # Trust measured dimensions over the model's guess.
        data["image_width"] = width
        data["image_height"] = height
        result = GroundingResult.model_validate(data)
        log.info("grounded %d top-level element(s)", len(result.elements))
        return result
```

---

## Phase 3 — LayoutPlanner (stage 2: text reasoning)

### Task 3.1 — Create planner.py with the prompt and class shell

- [ ] Create `services/skills/screenshot-to-component/planner.py`. The planner reasons over the grounding JSON (text only — no image) to produce a hierarchical layout plan, inferred color palette, and typography notes:

```python
"""LayoutPlanner: GroundingResult -> hierarchical LayoutPlan.

stdout is sacred — never print(). SINGLE-GPU — all calls via llm.call_llm.
"""
from __future__ import annotations

import logging

from llm import call_llm, extract_json
from models import GroundingResult, LayoutPlan

log = logging.getLogger("screenshot-to-component.planner")

PLANNING_SYSTEM = (
    "You are a frontend layout planner. Given detected UI elements (with bounding "
    "boxes and labels), produce a hierarchical layout plan for a React + Tailwind "
    "component. Return STRICT JSON only (no prose, no code fences).\n"
    "Schema:\n"
    '{ "root_component": <Tailwind classes for the outermost container, e.g. '
    '"flex flex-col min-h-screen">, "sections": [ { "name": <string>, '
    '"tailwind": <container classes>, "children": [ ...nested sections or leaf '
    'descriptions... ] } ], "color_palette": [<hex strings>], '
    '"typography_notes": <string> }\n'
    "Infer the layout direction (row/column), spacing, and a small color palette "
    "from the elements and their descriptions."
)


class LayoutPlanner:
    """Stage 2: turn grounded elements into a structured layout plan."""

    def __init__(self, model: str | None = None) -> None:
        self._model = model
```

### Task 3.2 — Implement LayoutPlanner.plan

- [ ] Append `plan`. It accepts either a `GroundingResult` object or a JSON string (the MCP `plan` tool passes a string), so the stage is usable both inside `generate()` and standalone:

```python
    def plan(self, grounding: GroundingResult | str) -> LayoutPlan:
        if isinstance(grounding, str):
            grounding = GroundingResult.model_validate_json(grounding)

        log.info("planning over %d top-level element(s)", len(grounding.elements))
        messages = [
            {"role": "system", "content": PLANNING_SYSTEM},
            {
                "role": "user",
                "content": (
                    "Detected elements (JSON):\n"
                    + grounding.model_dump_json()
                    + "\n\nProduce the layout plan JSON."
                ),
            },
        ]
        raw = call_llm(messages, temperature=0.2, max_tokens=2048)
        plan = LayoutPlan.model_validate(extract_json(raw))
        log.info("plan: %d section(s), %d palette color(s)",
                 len(plan.sections), len(plan.color_palette))
        return plan
```

---

## Phase 4 — ComponentGenerator (stage 3: code synthesis)

### Task 4.1 — Create generator.py with framework prompts and class shell

- [ ] Create `services/skills/screenshot-to-component/generator.py`. Framework-specific guidance lives in a dict so adding a framework is a one-line change. The default is shadcn/ui-compatible React + Tailwind:

```python
"""ComponentGenerator: LayoutPlan -> React/Tailwind (or other) component code.

stdout is sacred — never print(). SINGLE-GPU — all calls via llm.call_llm.
"""
from __future__ import annotations

import logging
from pathlib import Path

from llm import call_llm
from models import GenerationResult, LayoutPlan

log = logging.getLogger("screenshot-to-component.generator")

FRAMEWORK_GUIDANCE: dict[str, str] = {
    "react-tailwind": (
        "Generate a single self-contained React function component in TypeScript "
        "(.tsx). Use Tailwind utility classes. Prefer shadcn/ui component "
        "conventions and primitives where natural. Export the component as the "
        "default export. Do not include build config or imports for packages that "
        "are not standard React/Tailwind/shadcn."
    ),
    "html-css": (
        "Generate a single self-contained HTML file with an embedded <style> block "
        "using plain CSS (no Tailwind, no frameworks)."
    ),
    "vue-tailwind": (
        "Generate a single Vue 3 single-file component (<template>, <script setup>, "
        "no <style> needed) using Tailwind utility classes."
    ),
}

GENERATION_SYSTEM = (
    "You are a senior frontend engineer. Given a structured layout plan, write "
    "clean, production-quality component code. Output ONLY the code — no prose, no "
    "markdown fences, no explanation."
)


class ComponentGenerator:
    """Stage 3: synthesize component code from a layout plan."""

    def __init__(self, model: str | None = None) -> None:
        self._model = model
```

### Task 4.2 — Implement ComponentGenerator.generate

- [ ] Append `generate`. It validates the framework, builds the prompt from the plan, calls the LLM, strips any stray code fences the model adds, and writes to `output_path` when provided:

```python
    def generate(
        self,
        plan: LayoutPlan,
        framework: str = "react-tailwind",
        output_path: str | None = None,
    ) -> GenerationResult:
        if framework not in FRAMEWORK_GUIDANCE:
            raise ValueError(
                f"unsupported framework {framework!r}; "
                f"choose one of {sorted(FRAMEWORK_GUIDANCE)}"
            )

        log.info("generating %s component", framework)
        messages = [
            {"role": "system", "content": GENERATION_SYSTEM},
            {
                "role": "user",
                "content": (
                    FRAMEWORK_GUIDANCE[framework]
                    + "\n\nLayout plan (JSON):\n"
                    + plan.model_dump_json()
                    + "\n\nWrite the component now."
                ),
            },
        ]
        raw = call_llm(messages, temperature=0.2, max_tokens=8192)
        code = self._strip_fences(raw)

        if output_path:
            dest = Path(output_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(code, encoding="utf-8")
            log.info("wrote component to %s", output_path)

        return GenerationResult(
            component_code=code, framework=framework, output_path=output_path
        )

    @staticmethod
    def _strip_fences(text: str) -> str:
        """Remove a leading/trailing ```lang fence if the model added one."""
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            # drop the opening fence line
            lines = lines[1:]
            # drop the closing fence line if present
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            stripped = "\n".join(lines).strip()
        return stripped
```

---

## Phase 5 — Pipeline orchestration

### Task 5.1 — Create pipeline.py wiring the three stages

- [ ] Create `services/skills/screenshot-to-component/pipeline.py`. This is the single place where `generate()` chains grounder → planner → generator in order. The MCP `generate` tool calls this; tests assert the call order here. It returns a plain dict ready to be JSON-serialized over MCP:

```python
"""Pipeline: chains grounding -> planning -> generation.

stdout is sacred — never print(). SINGLE-GPU — every stage uses GEMMA_BASE.
"""
from __future__ import annotations

import logging

from generator import ComponentGenerator
from grounder import UIGrounder
from planner import LayoutPlanner

log = logging.getLogger("screenshot-to-component.pipeline")


class Pipeline:
    def __init__(
        self,
        grounder: UIGrounder | None = None,
        planner: LayoutPlanner | None = None,
        generator: ComponentGenerator | None = None,
    ) -> None:
        # Defaults are real stages; tests inject fakes to assert ordering.
        self.grounder = grounder or UIGrounder()
        self.planner = planner or LayoutPlanner()
        self.generator = generator or ComponentGenerator()

    def generate(
        self,
        image_path: str,
        framework: str = "react-tailwind",
        output_path: str | None = None,
    ) -> dict:
        # ORDER MATTERS: ground -> plan -> generate.
        grounding = self.grounder.ground(image_path)
        plan = self.planner.plan(grounding)
        gen = self.generator.generate(plan, framework=framework, output_path=output_path)
        return {
            "component_code": gen.component_code,
            "layout_plan": plan.model_dump(),
            "output_path": gen.output_path,
        }
```

---

## Phase 6 — MCP server entry point

### Task 6.1 — Create server.py with logging, imports, app

- [ ] Create `services/skills/screenshot-to-component/server.py`. Logging is configured to stderr FIRST, before importing litellm or any stage, so even import-time chatter cannot leak onto stdout:

```python
"""MCP stdio server for the screenshot_to_component skill.

CRITICAL: stdout is the JSON-RPC 2.0 channel. NEVER print() anywhere.
All logging is configured to sys.stderr below, including third-party loggers.
SINGLE-GPU: every LLM call targets GEMMA_BASE.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# CRITICAL: stderr only. Configure before importing anything that may log (litellm).
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("screenshot-to-component.server")

from grounder import UIGrounder  # noqa: E402 (after logging is configured)
from pipeline import Pipeline  # noqa: E402
from planner import LayoutPlanner  # noqa: E402

app: Server = Server("screenshot-to-component")
pipeline: Pipeline | None = None
```

### Task 6.2 — Implement list_tools

- [ ] Append the `list_tools` handler exposing all three tools with self-contained JSON Schemas. Note the dotted tool names are the public skill identifiers; the MCP `name` field uses the same dotted form so the bridge surfaces them verbatim:

```python
@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="screenshot_to_component.generate",
            description=(
                "Full 3-stage pipeline (grounding -> planning -> generation). "
                "Returns JSON with component_code, layout_plan, and output_path."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "image_path": {
                        "type": "string",
                        "description": "Absolute path to the screenshot/mockup image.",
                    },
                    "framework": {
                        "type": "string",
                        "enum": ["react-tailwind", "html-css", "vue-tailwind"],
                        "default": "react-tailwind",
                        "description": "Target framework for the generated component.",
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Optional absolute path to write the component file to.",
                    },
                },
                "required": ["image_path"],
            },
        ),
        Tool(
            name="screenshot_to_component.ground",
            description=(
                "Grounding stage only. Returns JSON with detected UI elements "
                "(bounding boxes + semantic labels). Useful for inspection."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "image_path": {
                        "type": "string",
                        "description": "Absolute path to the screenshot/mockup image.",
                    },
                },
                "required": ["image_path"],
            },
        ),
        Tool(
            name="screenshot_to_component.plan",
            description=(
                "Planning stage only. Takes grounding JSON, returns a "
                "hierarchical layout plan as JSON."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "grounding_result": {
                        "type": "string",
                        "description": "GroundingResult serialized as a JSON string.",
                    },
                },
                "required": ["grounding_result"],
            },
        ),
    ]
```

### Task 6.3 — Implement call_tool

- [ ] Append the `call_tool` dispatcher. It lazily constructs a `Pipeline` (and reuses its stages for the standalone tools), routes by tool name, serializes results to JSON, and returns errors as content rather than crashing the stdio server:

```python
@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    global pipeline
    if pipeline is None:
        pipeline = Pipeline()
        log.info("Pipeline initialized")

    try:
        if name == "screenshot_to_component.generate":
            result = pipeline.generate(
                arguments["image_path"],
                framework=arguments.get("framework", "react-tailwind"),
                output_path=arguments.get("output_path"),
            )
            text = json.dumps(result, ensure_ascii=False)
        elif name == "screenshot_to_component.ground":
            grounding = pipeline.grounder.ground(arguments["image_path"])
            text = grounding.model_dump_json()
        elif name == "screenshot_to_component.plan":
            plan = pipeline.planner.plan(arguments["grounding_result"])
            text = plan.model_dump_json()
        else:
            log.error("unknown tool: %s", name)
            return [TextContent(type="text", text=f"unknown tool: {name}")]
    except Exception as exc:  # never let one bad call crash the stdio server
        log.exception("tool %s failed", name)
        return [TextContent(type="text", text=f"error: {exc!r}")]

    return [TextContent(type="text", text=text)]
```

### Task 6.4 — Implement main and entry guard

- [ ] Append the async `main` and `__main__` guard:

```python
async def main() -> None:
    log.info("starting screenshot-to-component MCP server")
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Phase 7 — Tests (mocked, no GPU, no network)

### Task 7.1 — Create conftest.py with sys.path, a generated PNG fixture, and litellm stubs

- [ ] Create `tests/services/skills/screenshot-to-component/conftest.py`. It puts the server dir on `sys.path`, generates a tiny real PNG with Pillow, and provides a `fake_llm` fixture that monkeypatches `llm.call_llm` to return canned per-stage responses (so no network, no GPU):

```python
import json
import sys
from pathlib import Path

import pytest

SERVER_DIR = (
    Path(__file__).resolve().parents[4]
    / "services" / "skills" / "screenshot-to-component"
)
sys.path.insert(0, str(SERVER_DIR))


@pytest.fixture
def sample_image(tmp_path) -> str:
    """A tiny real PNG so encode_image_b64 reads genuine bytes."""
    from PIL import Image

    img = Image.new("RGB", (1440, 900), color=(255, 255, 255))
    path = tmp_path / "mock.png"
    img.save(path, format="PNG")
    return str(path)


GROUNDING_JSON = json.dumps(
    {
        "elements": [
            {
                "label": "header",
                "bounds": {"x": 0, "y": 0, "width": 1440, "height": 64},
                "description": "top nav bar",
                "children": [
                    {
                        "label": "button",
                        "bounds": {"x": 1300, "y": 16, "width": 120, "height": 32},
                        "description": "primary CTA button with blue background",
                    }
                ],
            }
        ]
    }
)

PLAN_JSON = json.dumps(
    {
        "root_component": "flex flex-col min-h-screen",
        "sections": [{"name": "header", "tailwind": "flex items-center justify-between"}],
        "color_palette": ["#1d4ed8", "#ffffff"],
        "typography_notes": "sans-serif, medium weight headings",
    }
)

COMPONENT_CODE = "export default function App() {\n  return <div className=\"flex\" />;\n}\n"


@pytest.fixture
def fake_llm(monkeypatch):
    """Patch llm.call_llm to return a stage-appropriate canned response.

    Records every call so tests can assert ordering and that images are sent.
    Routing is by message shape: a vision message (list content with image_url)
    => grounding; a 'layout plan' request => planning; otherwise generation.
    """
    import llm

    calls: list[dict] = []

    def _fake(messages, *, temperature=0.2, max_tokens=4096):
        calls.append({"messages": messages, "max_tokens": max_tokens})
        user = messages[-1]["content"]
        if isinstance(user, list):  # vision message -> grounding stage
            return GROUNDING_JSON
        if "layout plan" in user.lower() or "Detected elements" in user:
            return PLAN_JSON
        return COMPONENT_CODE

    monkeypatch.setattr(llm, "call_llm", _fake)
    # Stages import call_llm by name; patch their references too.
    for mod_name in ("grounder", "planner", "generator"):
        mod = __import__(mod_name)
        if hasattr(mod, "call_llm"):
            monkeypatch.setattr(mod, "call_llm", _fake)
    return calls
```

> The `for mod_name in (...)` loop matters: each stage does `from llm import call_llm`, binding the name into its own module namespace. Patching only `llm.call_llm` would not affect those bound references.

### Task 7.2 — test_grounder.py: base64 encoding + typed parse + no stdout

- [ ] Create `tests/services/skills/screenshot-to-component/test_grounder.py`:

```python
import pytest

from grounder import UIGrounder
from models import GroundingResult


@pytest.mark.mocked
def test_ground_encodes_image_as_base64_in_vision_message(sample_image, fake_llm):
    UIGrounder().ground(sample_image)
    # The grounding call must carry a base64 PNG data URL in the vision content.
    vision_call = fake_llm[0]
    content = vision_call["messages"][-1]["content"]
    assert isinstance(content, list)
    image_parts = [p for p in content if p.get("type") == "image_url"]
    assert image_parts, "no image_url part sent to the vision model"
    url = image_parts[0]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert len(url) > len("data:image/png;base64,")  # actual payload present


@pytest.mark.mocked
def test_ground_parses_llm_json_into_typed_element_tree(sample_image, fake_llm):
    result = UIGrounder().ground(sample_image)
    assert isinstance(result, GroundingResult)
    assert result.image_width == 1440 and result.image_height == 900  # measured, not guessed
    assert result.elements[0].label == "header"
    # nested child parsed into UIElement
    child = result.elements[0].children[0]
    assert child.label == "button"
    assert child.bounds.width == 120


@pytest.mark.mocked
def test_ground_raises_on_missing_image(fake_llm):
    with pytest.raises(FileNotFoundError):
        UIGrounder().ground("/does/not/exist.png")


@pytest.mark.mocked
def test_ground_writes_nothing_to_stdout(sample_image, fake_llm, capsys):
    UIGrounder().ground(sample_image)
    assert capsys.readouterr().out == ""
```

### Task 7.3 — test_planner.py: accepts string or object, typed plan, no stdout

- [ ] Create `tests/services/skills/screenshot-to-component/test_planner.py`:

```python
import pytest

from models import GroundingResult, LayoutPlan
from planner import LayoutPlanner


@pytest.mark.mocked
def test_plan_accepts_grounding_json_string(fake_llm):
    grounding_json = GroundingResult(
        elements=[], image_width=800, image_height=600
    ).model_dump_json()
    plan = LayoutPlanner().plan(grounding_json)  # string input (MCP plan tool path)
    assert isinstance(plan, LayoutPlan)
    assert plan.root_component == "flex flex-col min-h-screen"
    assert "#1d4ed8" in plan.color_palette


@pytest.mark.mocked
def test_plan_accepts_grounding_result_object(fake_llm):
    grounding = GroundingResult(elements=[], image_width=800, image_height=600)
    plan = LayoutPlanner().plan(grounding)  # object input (generate() path)
    assert isinstance(plan, LayoutPlan)
    assert plan.sections[0]["name"] == "header"


@pytest.mark.mocked
def test_plan_writes_nothing_to_stdout(fake_llm, capsys):
    LayoutPlanner().plan(GroundingResult(elements=[], image_width=10, image_height=10))
    assert capsys.readouterr().out == ""
```

### Task 7.4 — test_generator.py: framework validation, fence stripping, output_path write, no stdout

- [ ] Create `tests/services/skills/screenshot-to-component/test_generator.py`:

```python
import pytest

from generator import ComponentGenerator
from models import GenerationResult, LayoutPlan


def _plan() -> LayoutPlan:
    return LayoutPlan(root_component="flex", sections=[], color_palette=[], typography_notes="")


@pytest.mark.mocked
def test_generate_returns_component_code(fake_llm):
    res = ComponentGenerator().generate(_plan(), framework="react-tailwind")
    assert isinstance(res, GenerationResult)
    assert "export default" in res.component_code
    assert res.framework == "react-tailwind"
    assert res.output_path is None


@pytest.mark.mocked
def test_generate_rejects_unknown_framework(fake_llm):
    with pytest.raises(ValueError):
        ComponentGenerator().generate(_plan(), framework="svelte-runes")


@pytest.mark.mocked
def test_generate_writes_output_path_when_provided(tmp_path, fake_llm):
    out = tmp_path / "nested" / "App.tsx"
    res = ComponentGenerator().generate(_plan(), output_path=str(out))
    assert out.is_file()
    assert out.read_text(encoding="utf-8") == res.component_code


@pytest.mark.mocked
def test_generate_strips_code_fences(fake_llm, monkeypatch):
    import generator
    monkeypatch.setattr(
        generator, "call_llm",
        lambda *a, **k: "```tsx\nexport default function A(){return null}\n```",
    )
    res = ComponentGenerator().generate(_plan())
    assert not res.component_code.startswith("```")
    assert res.component_code.startswith("export default")


@pytest.mark.mocked
def test_generate_writes_nothing_to_stdout(fake_llm, capsys):
    ComponentGenerator().generate(_plan())
    assert capsys.readouterr().out == ""
```

### Task 7.5 — test_pipeline.py: chaining order and full result shape

- [ ] Create `tests/services/skills/screenshot-to-component/test_pipeline.py`. This asserts `generate()` calls the stages in order (ground → plan → generate) using spy stages, and that the assembled dict has the expected keys:

```python
import pytest

from models import GenerationResult, GroundingResult, LayoutPlan
from pipeline import Pipeline


class _SpyGrounder:
    def __init__(self, log): self.log = log
    def ground(self, image_path):
        self.log.append("ground")
        return GroundingResult(elements=[], image_width=1, image_height=1)


class _SpyPlanner:
    def __init__(self, log): self.log = log
    def plan(self, grounding):
        self.log.append("plan")
        return LayoutPlan()


class _SpyGenerator:
    def __init__(self, log): self.log = log
    def generate(self, plan, framework="react-tailwind", output_path=None):
        self.log.append("generate")
        return GenerationResult(
            component_code="CODE", framework=framework, output_path=output_path
        )


@pytest.mark.mocked
def test_generate_chains_stages_in_order():
    log: list[str] = []
    pipe = Pipeline(_SpyGrounder(log), _SpyPlanner(log), _SpyGenerator(log))
    result = pipe.generate("/x.png", framework="react-tailwind", output_path="/out.tsx")
    assert log == ["ground", "plan", "generate"]
    assert set(result) == {"component_code", "layout_plan", "output_path"}
    assert result["component_code"] == "CODE"
    assert result["output_path"] == "/out.tsx"
    assert isinstance(result["layout_plan"], dict)  # LayoutPlan dumped to dict


@pytest.mark.mocked
def test_generate_end_to_end_with_fake_llm(sample_image, fake_llm, tmp_path):
    out = tmp_path / "App.tsx"
    result = Pipeline().generate(sample_image, output_path=str(out))
    # ground -> plan -> generate produced three LLM calls, vision first.
    assert len(fake_llm) == 3
    assert isinstance(fake_llm[0]["messages"][-1]["content"], list)  # vision call first
    assert out.is_file()
    assert result["layout_plan"]["root_component"] == "flex flex-col min-h-screen"
```

### Task 7.6 — test_server.py: tool registration and dispatch (no stdout)

- [ ] Create `tests/services/skills/screenshot-to-component/test_server.py`. It exercises the registered `list_tools`/`call_tool` handlers directly (no subprocess) and asserts dotted tool names and JSON-parseable output:

```python
import json

import pytest


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_list_tools_exposes_three_dotted_tools():
    import server
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "screenshot_to_component.generate",
        "screenshot_to_component.ground",
        "screenshot_to_component.plan",
    }


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_call_tool_generate_returns_json(sample_image, fake_llm):
    import server
    server.pipeline = None  # force fresh construction
    out = await server.call_tool(
        "screenshot_to_component.generate", {"image_path": sample_image}
    )
    payload = json.loads(out[0].text)
    assert set(payload) == {"component_code", "layout_plan", "output_path"}


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_call_tool_unknown_returns_error_content_not_raise(fake_llm):
    import server
    out = await server.call_tool("screenshot_to_component.bogus", {})
    assert "unknown tool" in out[0].text


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_call_tool_writes_nothing_to_stdout(sample_image, fake_llm, capsys):
    import server
    server.pipeline = None
    await server.call_tool("screenshot_to_component.ground", {"image_path": sample_image})
    assert capsys.readouterr().out == ""
```

> If the repo's pytest config does not already enable asyncio mode, add `asyncio_mode = auto` under `[tool.pytest.ini_options]`, or decorate with the project's existing async-test convention.

---

## Phase 8 — Verification checklist

- [ ] Grep the server dir for stdout violations — must return nothing:

```bash
grep -rnE '(^|[^.])\bprint\(' /Users/zachstallbohm/Work/gemma/services/skills/screenshot-to-component
```

- [ ] Grep for forbidden tiktoken — must return nothing:

```bash
grep -rn 'tiktoken' /Users/zachstallbohm/Work/gemma/services/skills/screenshot-to-component
```

- [ ] Grep for a stray QWEN reference — must return nothing (single-GPU):

```bash
grep -rni 'qwen' /Users/zachstallbohm/Work/gemma/services/skills/screenshot-to-component
```

- [ ] Confirm every LLM call routes through GEMMA_BASE (only `llm.py` should reference it):

```bash
grep -rn 'GEMMA_BASE\|api_base' /Users/zachstallbohm/Work/gemma/services/skills/screenshot-to-component
```

- [ ] Run the mocked test suite (no GPU, no network):

```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest tests/services/skills/screenshot-to-component -m mocked -q
```

- [ ] Smoke-test the server starts and speaks JSON-RPC over stdio (initialize handshake). It must print nothing to stdout except framed JSON:

```bash
cd /Users/zachstallbohm/Work/gemma/services/skills/screenshot-to-component && \
python - <<'PY'
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
| `screenshot_to_component.generate` tool (full pipeline, JSON out) | 5.1, 6.2, 6.3, 7.5, 7.6 |
| `screenshot_to_component.ground` tool (grounding only) | 2.2, 6.2, 6.3, 7.2 |
| `screenshot_to_component.plan` tool (string in, plan out) | 3.2, 6.2, 6.3, 7.3 |
| 3-stage modular pipeline (ScreenCoder) | 2.x, 3.x, 4.x, 5.1 |
| UIGrounder: Gemma 4 vision, bbox + labels | 2.1, 2.2 |
| Image sent as base64 PNG in vision content | 1.2 (encode), 2.2, 7.2 |
| GroundingResult typed UIElement tree (nested children) | 1.1, 2.2, 7.2 |
| LayoutPlanner: text reasoning over grounding | 3.1, 3.2 |
| plan() accepts JSON string OR object | 3.2, 7.3 |
| ComponentGenerator: plan -> code | 4.1, 4.2 |
| framework: react-tailwind \| html-css \| vue-tailwind | 4.1, 4.2, 7.4 |
| output_path written when provided | 4.2, 7.4, 7.5 |
| generate() chains ground -> plan -> generate in order | 5.1, 7.5 |
| Pydantic key types (BoundingBox/UIElement/GroundingResult/LayoutPlan/GenerationResult) | 1.1 |
| stderr-only logging, never stdout | 1.1, 1.2, 6.1, 7.2/7.3/7.4/7.6, Phase 8 |
| Single-GPU: all calls via GEMMA_BASE, no QWEN_BASE | 1.2, Phase 8 |
| No tiktoken | Phase 8 |
| SKILL.md frontmatter (exact) | 0.3 |
| requires: [react-doctor] / chaining | 0.3 |
| stdio transport child process | 6.1, 6.4, Phase 8 |
| All tests mocked (@pytest.mark.mocked) | 7.2–7.6 |

---

## Notes for the implementer

- **litellm vision message shape is the integration risk.** vLLM serving Gemma 4 with an OpenAI-compatible API expects the standard `image_url` content part (`{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}`). If a future litellm/vLLM version changes the expected shape, only `grounder.ground` (Task 2.2) needs adjusting — the encoder in `llm.py` already returns a clean base64 payload.
- **JSON robustness.** Gemma may wrap JSON in prose or code fences despite the "STRICT JSON only" instruction; `extract_json` (Task 1.2) handles both. If parsing still fails in practice, lower temperature or add a one-shot example to the system prompt rather than loosening the parser.
- **Single-GPU is enforced structurally**, not by convention: only `llm.py` knows the endpoint and model. No stage hardcodes a base URL, and there is no QWEN path to accidentally select. The Phase 8 grep for `qwen` guards against regressions.
- **Measured image dimensions override the model's guess** in `ground` (Task 2.2), because downstream Tailwind sizing and the planner rely on accurate `image_width`/`image_height`.
- **Chaining with react-doctor** is declared in SKILL.md (`requires: [react-doctor]`); the orchestrator is expected to run react-doctor on the generated component. This skill does not invoke react-doctor itself.
