# design-critique MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the design-critique Python MCP server — Gemma 4 vision critique of UI screenshots returning structured per-area checklists with pass/fail/warning status.

**Architecture:** UICritic encodes the image as base64 and calls Gemma 4 with a structured-output prompt requesting JSON critique across configurable focus areas. Returns a typed CritiqueResult with per-item ChecklistItems. The compare tool sends both images in one vision call and requests a diff-focused critique. All logging to stderr.

**Tech Stack:** Python 3.11+, `mcp` SDK, `litellm`, `pydantic>=2`, `Pillow`, `pytest`

---

## Background

This is a **single-shot Gemma 4 vision skill**. There is no multi-step pipeline, no
retrieval, and no external service beyond the vLLM inference server. The orchestrator
calls it as a self-review gate after generating a UI component, or standalone to audit
an existing design.

**Two tools exposed:**
- `design_critique.critique(image_path, focus_areas?)` — critique one UI screenshot, returns JSON `CritiqueResult`
- `design_critique.compare(before_path, after_path)` — compare two screenshots, returns JSON diff critique

**Focus areas (the canonical set):**
`visual_hierarchy`, `spacing_alignment`, `color_contrast`, `typography`,
`layout_balance`, `responsive_concerns`, `accessibility_surface`

When `focus_areas` is `None`, all seven are checked. When a subset is provided, only
those areas are critiqued and `focus_areas_checked` reflects the filtered set.

**Critical rules (non-negotiable):**
- **stdout is sacred:** ALL logging via `logging` to `sys.stderr`. NEVER `print()`. stdout carries JSON-RPC 2.0.
- **Single-GPU:** `GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")` for all LLM calls. Never hardcode a second model/base.
- Images encoded as **base64 PNG** for Gemma 4 vision (re-encode through Pillow so any input format becomes PNG).
- Output is **structured JSON**, not prose — each focus area contributes `ChecklistItem`s with `status: pass|fail|warning`, `severity`, and `note`.
- `asyncio.run` only at module top-level under `__main__` — never inside a running loop.
- Python files: `snake_case.py`. Classes: PascalCase.

---

## Phase 1 — Project scaffolding

### Task 1.1 — Create directory structure

- [ ] Create the skill and test directory trees:

```bash
mkdir -p services/skills/design-critique
mkdir -p tests/services/skills/design-critique
```

Resulting layout:

```
services/skills/design-critique/
  server.py
  critic.py
  SKILL.md
  requirements.txt
tests/services/skills/design-critique/
  test_critic.py
  conftest.py
```

### Task 1.2 — Write requirements.txt

- [ ] Create `services/skills/design-critique/requirements.txt`:

```
mcp>=1.0.0
litellm>=1.40.0
pydantic>=2.0
Pillow>=10.0.0
```

### Task 1.3 — Write SKILL.md

- [ ] Create `services/skills/design-critique/SKILL.md`:

```markdown
---
name: design-critique
description: >
  Structured UX/visual critique of UI screenshots using Gemma 4 vision.
  Returns a per-area checklist (visual hierarchy, spacing, contrast, typography,
  layout, accessibility surface) with pass/fail/warning status per item.
  Use as a self-review step after generating UI components, or standalone to
  audit an existing design.
trigger: "Use when reviewing a UI design, screenshot, or rendered component for quality"
tools:
  - design_critique.critique
  - design_critique.compare
version: "0.1.0"
license: MIT
requires: []
---

# design-critique

Single-shot Gemma 4 vision critique of UI screenshots.

## Tools

### `design_critique.critique(image_path, focus_areas?)`
Critique one UI screenshot. `focus_areas` is an optional subset of:
`visual_hierarchy`, `spacing_alignment`, `color_contrast`, `typography`,
`layout_balance`, `responsive_concerns`, `accessibility_surface`.
Returns a JSON `CritiqueResult` with a per-item checklist and an overall verdict.

### `design_critique.compare(before_path, after_path)`
Send a before/after pair in one vision call and return a diff-focused critique
(what improved, what regressed, what is still unresolved).

## Notes
- Images are re-encoded to base64 PNG before being sent to Gemma 4.
- Output is JSON, never prose. Each item carries `status`, `severity`, and a `note`.
```

---

## Phase 2 — Pydantic models and the UICritic class

### Task 2.1 — Define the focus-area constant and Pydantic models in `critic.py`

- [ ] Create `services/skills/design-critique/critic.py` with the module header, logging wired to stderr, the canonical focus-area list, and the typed models:

```python
"""design-critique — Gemma 4 vision critique of UI screenshots."""
from __future__ import annotations

import base64
import io
import logging
import os
import sys
from typing import Literal

import litellm
from PIL import Image
from pydantic import BaseModel

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("design-critique.critic")

GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")
GEMMA_MODEL = os.getenv("GEMMA_MODEL", "openai/google/gemma-4-31B-it")

FOCUS_AREAS: list[str] = [
    "visual_hierarchy",
    "spacing_alignment",
    "color_contrast",
    "typography",
    "layout_balance",
    "responsive_concerns",
    "accessibility_surface",
]


class ChecklistItem(BaseModel):
    issue: str
    status: Literal["pass", "fail", "warning"]
    note: str
    severity: Literal["high", "medium", "low"]


class CritiqueResult(BaseModel):
    image_path: str
    focus_areas_checked: list[str]
    items: list[ChecklistItem]
    overall: Literal["pass", "needs_work", "fail"]
    summary: str  # one-sentence overall verdict
```

> **Note:** `GEMMA_MODEL` is prefixed `openai/` so litellm routes to the
> OpenAI-compatible vLLM endpoint at `GEMMA_BASE`.

### Task 2.2 — Implement `_encode_image` (base64 PNG)

- [ ] Add the image encoder to `UICritic`. Re-encode through Pillow so any input
      format (JPEG, WebP, PNG) becomes PNG, then base64. Begin the class:

```python
class UICritic:
    def __init__(self, model: str = GEMMA_MODEL, api_base: str = GEMMA_BASE) -> None:
        self.model = model
        self.api_base = api_base

    def _encode_image(self, path: str) -> str:
        """Load any image, re-encode as PNG, return base64 string."""
        with Image.open(path) as img:
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")
```

### Task 2.3 — Implement `_call_gemma_vision`

- [ ] Add the vision call. It accepts a list of base64 PNG strings and a prompt,
      builds an OpenAI-style multimodal message, and returns the raw text content:

```python
    def _call_gemma_vision(self, images: list[str], prompt: str) -> str:
        content: list[dict] = [{"type": "text", "text": prompt}]
        for b64 in images:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                }
            )
        resp = litellm.completion(
            model=self.model,
            api_base=self.api_base,
            messages=[{"role": "user", "content": content}],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        return resp["choices"][0]["message"]["content"]
```

> **Note:** `response_format={"type": "json_object"}` asks vLLM/Gemma for a JSON
> body. We still defensively parse and validate below — never trust the shape blind.

### Task 2.4 — Implement the critique prompt builder

- [ ] Add a helper that builds the structured-output instruction for a given set
      of focus areas. It enumerates the requested areas and pins the exact JSON schema:

```python
    def _critique_prompt(self, areas: list[str]) -> str:
        area_lines = "\n".join(f"- {a}" for a in areas)
        return (
            "You are a senior UI/UX reviewer. Critique the attached UI screenshot.\n"
            "Check ONLY these focus areas:\n"
            f"{area_lines}\n\n"
            "Return a SINGLE JSON object with this exact shape:\n"
            "{\n"
            '  "items": [\n'
            '    {"issue": str, "status": "pass"|"fail"|"warning",\n'
            '     "note": str, "severity": "high"|"medium"|"low"}\n'
            "  ],\n"
            '  "overall": "pass"|"needs_work"|"fail",\n'
            '  "summary": str\n'
            "}\n"
            "Produce at least one item per focus area. Keep notes concrete and "
            "actionable. Output JSON only, no prose, no markdown fences."
        )
```

### Task 2.5 — Implement `critique`

- [ ] Add the `critique` method. Resolve focus areas, encode the image, call Gemma,
      parse + validate into `CritiqueResult`, and attach the resolved metadata:

```python
    def critique(
        self, image_path: str, focus_areas: list[str] | None = None
    ) -> CritiqueResult:
        areas = self._resolve_areas(focus_areas)
        log.info("critiquing %s across %d areas", image_path, len(areas))
        b64 = self._encode_image(image_path)
        raw = self._call_gemma_vision([b64], self._critique_prompt(areas))
        data = self._parse_json(raw)
        items = [ChecklistItem(**i) for i in data.get("items", [])]
        return CritiqueResult(
            image_path=image_path,
            focus_areas_checked=areas,
            items=items,
            overall=data.get("overall", "needs_work"),
            summary=data.get("summary", ""),
        )
```

### Task 2.6 — Implement `compare`

- [ ] Add the `compare` method. Encode both images, send them in ONE vision call
      with a diff-focused prompt, and return a parsed dict:

```python
    def compare(self, before_path: str, after_path: str) -> dict:
        log.info("comparing %s -> %s", before_path, after_path)
        before_b64 = self._encode_image(before_path)
        after_b64 = self._encode_image(after_path)
        prompt = (
            "You are a senior UI/UX reviewer. Image 1 is BEFORE, image 2 is AFTER "
            "a UI change. Critique the diff: what improved, what regressed, what is "
            "still unresolved.\n"
            "Return a SINGLE JSON object:\n"
            "{\n"
            '  "improved": [str], "regressed": [str], "unresolved": [str],\n'
            '  "overall": "pass"|"needs_work"|"fail", "summary": str\n'
            "}\n"
            "Output JSON only, no prose, no markdown fences."
        )
        raw = self._call_gemma_vision([before_b64, after_b64], prompt)
        result = self._parse_json(raw)
        result["before_path"] = before_path
        result["after_path"] = after_path
        return result
```

### Task 2.7 — Implement the `_resolve_areas` and `_parse_json` helpers

- [ ] Add the two private helpers. `_resolve_areas` defaults to all areas and filters
      out unknown names; `_parse_json` strips accidental markdown fences before loading:

```python
    def _resolve_areas(self, focus_areas: list[str] | None) -> list[str]:
        if not focus_areas:
            return list(FOCUS_AREAS)
        return [a for a in focus_areas if a in FOCUS_AREAS] or list(FOCUS_AREAS)

    @staticmethod
    def _parse_json(raw: str) -> dict:
        import json

        text = raw.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip("` \n")
        return json.loads(text)
```

> **Note:** `import json` is local to keep the module top clean; it is hot-path-cheap.

---

## Phase 3 — MCP server

### Task 3.1 — Write `server.py` exposing both tools over stdio

- [ ] Create `services/skills/design-critique/server.py`. Register both tools,
      serialize results to JSON strings, run over stdio. stdout is JSON-RPC only.

```python
"""design-critique MCP server — exposes critique and compare over stdio."""
from __future__ import annotations

import json
import logging
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from critic import FOCUS_AREAS, UICritic

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("design-critique.server")

app = Server("design-critique")
critic = UICritic()


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="design_critique.critique",
            description=(
                "Critique a UI screenshot. Returns a per-area checklist with "
                "pass/fail/warning status per item and an overall verdict."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "image_path": {"type": "string"},
                    "focus_areas": {
                        "type": "array",
                        "items": {"type": "string", "enum": FOCUS_AREAS},
                    },
                },
                "required": ["image_path"],
            },
        ),
        Tool(
            name="design_critique.compare",
            description=(
                "Compare two UI screenshots (before/after) and return a "
                "diff-focused critique as JSON."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "before_path": {"type": "string"},
                    "after_path": {"type": "string"},
                },
                "required": ["before_path", "after_path"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "design_critique.critique":
        result = critic.critique(
            arguments["image_path"], arguments.get("focus_areas")
        )
        return [TextContent(type="text", text=result.model_dump_json())]
    if name == "design_critique.compare":
        result = critic.compare(arguments["before_path"], arguments["after_path"])
        return [TextContent(type="text", text=json.dumps(result))]
    raise ValueError(f"unknown tool: {name}")


async def main() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
```

> **Note:** `asyncio.run` is at module top-level under `__main__` only — never inside
> an already-running event loop (project rule).

---

## Phase 4 — Tests (all `@pytest.mark.mocked`)

### Task 4.1 — Write `conftest.py` with a mocked litellm vision call and a sample image

- [ ] Create `tests/services/skills/design-critique/conftest.py`. It patches
      `litellm.completion` so no GPU/network is touched, captures the messages sent,
      and provides a real temporary PNG so `_encode_image` exercises Pillow:

```python
import json
from pathlib import Path

import pytest
from PIL import Image


@pytest.fixture
def sample_png(tmp_path) -> str:
    p = tmp_path / "ui.png"
    Image.new("RGB", (64, 48), (200, 200, 200)).save(p, format="PNG")
    return str(p)


@pytest.fixture
def sample_jpeg(tmp_path) -> str:
    p = tmp_path / "ui.jpg"
    Image.new("RGB", (64, 48), (10, 20, 30)).save(p, format="JPEG")
    return str(p)


@pytest.fixture
def captured_calls():
    return []


@pytest.fixture
def mock_vision(monkeypatch, captured_calls):
    """Patch litellm.completion. Records kwargs; returns a canned JSON body."""
    import critic as critic_mod

    payload = {
        "items": [
            {
                "issue": "Primary CTA blends into background",
                "status": "fail",
                "note": "Increase contrast or use accent color.",
                "severity": "high",
            }
        ],
        "overall": "needs_work",
        "summary": "Solid layout, contrast needs work.",
    }

    def fake_completion(**kwargs):
        captured_calls.append(kwargs)
        return {
            "choices": [{"message": {"content": json.dumps(payload)}}]
        }

    monkeypatch.setattr(critic_mod.litellm, "completion", fake_completion)
    return payload
```

### Task 4.2 — Test: `critique()` returns a `CritiqueResult` with items

- [ ] Add to `tests/services/skills/design-critique/test_critic.py`:

```python
import pytest

from critic import FOCUS_AREAS, CritiqueResult, UICritic


@pytest.mark.mocked
def test_critique_returns_result_with_items(mock_vision, sample_png):
    result = UICritic().critique(sample_png)
    assert isinstance(result, CritiqueResult)
    assert result.image_path == sample_png
    assert len(result.items) >= 1
    assert result.items[0].status in {"pass", "fail", "warning"}
    assert result.items[0].severity in {"high", "medium", "low"}
```

### Task 4.3 — Test: default checks all focus areas; subset filters them

- [ ] Add:

```python
@pytest.mark.mocked
def test_default_checks_all_focus_areas(mock_vision, sample_png):
    result = UICritic().critique(sample_png)
    assert result.focus_areas_checked == FOCUS_AREAS


@pytest.mark.mocked
def test_focus_areas_filter_limits_checked(mock_vision, sample_png):
    subset = ["color_contrast", "typography"]
    result = UICritic().critique(sample_png, focus_areas=subset)
    assert result.focus_areas_checked == subset


@pytest.mark.mocked
def test_unknown_focus_area_falls_back_to_all(mock_vision, sample_png):
    result = UICritic().critique(sample_png, focus_areas=["not_a_real_area"])
    assert result.focus_areas_checked == FOCUS_AREAS
```

### Task 4.4 — Test: the prompt enumerates only the requested areas

- [ ] Add. Inspect the captured message text to confirm only requested areas appear:

```python
@pytest.mark.mocked
def test_prompt_contains_only_requested_areas(mock_vision, captured_calls, sample_png):
    UICritic().critique(sample_png, focus_areas=["typography"])
    text = captured_calls[0]["messages"][0]["content"][0]["text"]
    assert "typography" in text
    assert "color_contrast" not in text
```

### Task 4.5 — Test: image is encoded as base64 PNG (even from JPEG input)

- [ ] Add. Confirm the data URL is PNG and the base64 decodes to PNG magic bytes:

```python
import base64


@pytest.mark.mocked
def test_image_encoded_as_base64_png(mock_vision, captured_calls, sample_jpeg):
    UICritic().critique(sample_jpeg)
    content = captured_calls[0]["messages"][0]["content"]
    image_part = next(c for c in content if c["type"] == "image_url")
    url = image_part["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    raw = base64.b64decode(url.split(",", 1)[1])
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
```

### Task 4.6 — Test: `compare()` sends both images in one vision call

- [ ] Add:

```python
@pytest.mark.mocked
def test_compare_sends_both_images(mock_vision, captured_calls, sample_png, sample_jpeg):
    result = UICritic().compare(sample_png, sample_jpeg)
    content = captured_calls[0]["messages"][0]["content"]
    image_parts = [c for c in content if c["type"] == "image_url"]
    assert len(image_parts) == 2
    assert result["before_path"] == sample_png
    assert result["after_path"] == sample_jpeg
```

### Task 4.7 — Test: overall verdict is one of the allowed values

- [ ] Add:

```python
@pytest.mark.mocked
def test_overall_verdict_is_valid(mock_vision, sample_png):
    result = UICritic().critique(sample_png)
    assert result.overall in {"pass", "needs_work", "fail"}
```

### Task 4.8 — Test: GEMMA_BASE is used and never hardcoded elsewhere

- [ ] Add. Confirm the call routes to the env-configured base:

```python
@pytest.mark.mocked
def test_uses_gemma_base(mock_vision, captured_calls, sample_png, monkeypatch):
    monkeypatch.setenv("GEMMA_BASE", "http://host.docker.internal:8000/v1")
    import importlib
    import critic as critic_mod
    importlib.reload(critic_mod)
    # re-patch after reload
    monkeypatch.setattr(critic_mod.litellm, "completion",
                        lambda **kw: captured_calls.append(kw) or
                        {"choices": [{"message": {"content": '{"items":[],'
                         '"overall":"pass","summary":"ok"}'}}]})
    critic_mod.UICritic().critique(sample_png)
    assert captured_calls[-1]["api_base"] == "http://host.docker.internal:8000/v1"
```

### Task 4.9 — Test: no stdout writes during a critique

- [ ] Add. Logging must go to stderr only — stdout stays clean for JSON-RPC:

```python
@pytest.mark.mocked
def test_no_stdout_writes(mock_vision, sample_png, capsys):
    UICritic().critique(sample_png)
    captured = capsys.readouterr()
    assert captured.out == ""
```

### Task 4.10 — Test: markdown-fenced JSON is still parsed

- [ ] Add. The model sometimes wraps JSON in ```json fences; `_parse_json` must cope:

```python
@pytest.mark.mocked
def test_parses_fenced_json(monkeypatch, sample_png):
    import critic as critic_mod

    fenced = '```json\n{"items": [], "overall": "pass", "summary": "ok"}\n```'
    monkeypatch.setattr(
        critic_mod.litellm, "completion",
        lambda **kw: {"choices": [{"message": {"content": fenced}}]},
    )
    result = critic_mod.UICritic().critique(sample_png)
    assert result.overall == "pass"
```

---

## Phase 5 — Verification

### Task 5.1 — Install and run the test suite

- [ ] From the skill directory, install deps and run only the mocked tests:

```bash
cd services/skills/design-critique
python -m pip install -r requirements.txt
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skills/design-critique -m mocked -v
```

- [ ] Confirm all tests pass and no test required a GPU or network.

### Task 5.2 — Smoke-test the MCP server boots without writing to stdout

- [ ] Verify the server starts, advertises both tools, and emits nothing on stdout
      before JSON-RPC traffic. A quick check: pipe an `initialize` request and confirm
      the first stdout bytes are valid JSON-RPC, with all log lines on stderr.

```bash
cd services/skills/design-critique
python -c "import server; print('imports clean', file=__import__('sys').stderr)"
```

- [ ] Confirm the import prints nothing to stdout (the message above goes to stderr).

---

## Done criteria

- [ ] `UICritic.critique` returns a typed `CritiqueResult` with per-item checklist.
- [ ] `focus_areas` defaults to all seven and filters correctly when a subset is given.
- [ ] `compare` sends both images in a single vision call and returns a diff dict.
- [ ] All images sent as base64 PNG regardless of input format.
- [ ] All logging on stderr; stdout carries only JSON-RPC.
- [ ] `GEMMA_BASE` env-driven; no hardcoded inference URL.
- [ ] All tests `@pytest.mark.mocked` and green with no GPU/network.
