# B2 rebuttal-response Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan.

**Goal:** Add a `rebuttal-response` skill that decomposes reviewer comments into a concern matrix, drafts point-by-point replies, and audits coverage.

**Architecture:** A standard Labmate skill under `services/skills/rebuttal-response/`: `SKILL.md`, a logic module `rebuttal.py` with three functions, and an MCP `server.py`. `parse_reviews` and `coverage_audit` are deterministic; `draft_response` calls Gemma 4 31B (one call per concern) via litellm. `SkillRunner.discover()` catalogs it from frontmatter.

**Tech Stack:** Python, `mcp` SDK, `litellm` (Gemma 4 31B), pytest.

> **Skill rules:** Never `print()` — log to `sys.stderr`. `server.py` uses `mcp.server.Server` + `stdio_server`. Every `litellm.acompletion` MUST pass `api_key="not-needed"`, `model="openai/gemma-4-31b"`, and `api_base` from env `GEMMA_BASE` (default `http://localhost:8000/v1`), with an explicit `extra_body={"thinking_budget_tokens": ...}`.

---

### Task 1: Create the skill logic module

**Files:**
- Create: `services/skills/rebuttal-response/rebuttal.py`
- Create: `services/skills/rebuttal-response/__init__.py`
- Create: `tests/services/skills/rebuttal-response/__init__.py`
- Create: `tests/services/skills/rebuttal-response/test_rebuttal.py`

- [ ] **Step 1: Write the failing test**

Create `tests/services/skills/rebuttal-response/__init__.py` (empty), then `tests/services/skills/rebuttal-response/test_rebuttal.py`:

```python
from __future__ import annotations
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[3]
    / "services" / "skills" / "rebuttal-response" / "rebuttal.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("rebuttal", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parse_reviews_extracts_concerns():
    rb = _load()
    review = (
        "1. The evaluation is weak; no baseline comparison. (major)\n"
        "2. Typo in Section 3.\n"
    )
    out = rb.parse_reviews(review)
    assert "concerns" in out
    assert len(out["concerns"]) >= 2
    first = out["concerns"][0]
    assert set(first) >= {"id", "severity", "type", "target_section", "text"}


@pytest.mark.asyncio
async def test_draft_response_calls_llm_per_concern():
    rb = _load()
    concerns = [
        {"id": "c1", "severity": "major", "type": "evaluation",
         "target_section": "5", "text": "no baseline"},
        {"id": "c2", "severity": "minor", "type": "typo",
         "target_section": "3", "text": "typo"},
    ]
    fake = MagicMock()
    fake.choices = [MagicMock()]
    fake.choices[0].message.content = "We have addressed this by adding a baseline."
    with patch.object(rb.litellm, "acompletion", new=AsyncMock(return_value=fake)) as mac:
        out = await rb.draft_response(concerns, paper_context="Our method ...")
    assert mac.await_count == 2
    # Every call must pass api_key="not-needed".
    for call in mac.await_args_list:
        assert call.kwargs.get("api_key") == "not-needed"
    assert len(out["responses"]) == 2
    assert out["responses"][0]["concern_id"] == "c1"


def test_coverage_audit_reports_gaps():
    rb = _load()
    concerns = [{"id": "c1"}, {"id": "c2"}, {"id": "c3"}]
    responses = [
        {"concern_id": "c1", "response": "..."},
        {"concern_id": "c2", "response": "..."},
    ]
    out = rb.coverage_audit(concerns, responses)
    assert out["covered"] == ["c1", "c2"]
    assert out["gaps"] == ["c3"]
    assert abs(out["coverage_pct"] - (2 / 3)) < 1e-6
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/skills/rebuttal-response/test_rebuttal.py -v`
Expected: FAIL (module does not exist)

- [ ] **Step 3: Write minimal implementation**

Create `services/skills/rebuttal-response/__init__.py` (empty).

Create `services/skills/rebuttal-response/rebuttal.py`:

```python
"""rebuttal-response skill logic: parse reviews, draft replies, audit coverage.

CRITICAL: never write to stdout. All logging goes to stderr.
"""
from __future__ import annotations

import logging
import os
import re
import sys

import litellm

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("rebuttal-response")

GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")
_SECTION_RE = re.compile(r"section\s+(\d+(?:\.\d+)?)", re.IGNORECASE)


def parse_reviews(review_text: str) -> dict:
    """Decompose reviewer text into an itemized concern matrix."""
    concerns: list[dict] = []
    # Split on enumerated bullets ("1.", "2)", "- ") or blank lines.
    chunks = re.split(r"(?m)^\s*(?:\d+[.)]|[-*])\s+", review_text)
    chunks = [c.strip() for c in chunks if c.strip()]
    if not chunks:
        chunks = [p.strip() for p in review_text.split("\n\n") if p.strip()]
    for i, chunk in enumerate(chunks):
        low = chunk.lower()
        if "major" in low or "critical" in low or "weak" in low:
            severity = "major"
        else:
            severity = "minor"
        if "typo" in low or "grammar" in low or "wording" in low:
            ctype = "presentation"
        elif "experiment" in low or "baseline" in low or "evaluation" in low:
            ctype = "evaluation"
        elif "cite" in low or "reference" in low or "related work" in low:
            ctype = "related_work"
        else:
            ctype = "general"
        sec_match = _SECTION_RE.search(chunk)
        target = sec_match.group(1) if sec_match else ""
        concerns.append({
            "id": f"c{i + 1}",
            "severity": severity,
            "type": ctype,
            "target_section": target,
            "text": chunk,
        })
    return {"concerns": concerns}


async def draft_response(concerns: list[dict], paper_context: str) -> dict:
    """Generate a point-by-point reply per concern via Gemma 4 31B."""
    responses: list[dict] = []
    for concern in concerns:
        prompt = (
            "You are drafting an author response to a peer-review concern.\n"
            f"PAPER CONTEXT:\n{paper_context}\n\n"
            f"REVIEWER CONCERN ({concern.get('severity', 'minor')}, "
            f"{concern.get('type', 'general')}): {concern.get('text', '')}\n\n"
            "Write a concise, respectful, point-by-point reply grounded in the paper. "
            "Do not invent results not in the context."
        )
        try:
            r = await litellm.acompletion(
                model="openai/gemma-4-31b",
                api_base=GEMMA_BASE,
                api_key="not-needed",
                messages=[{"role": "user", "content": prompt}],
                extra_body={"thinking_budget_tokens": 1024},
            )
            text = r.choices[0].message.content or ""
        except Exception as exc:
            log.warning("draft_response failed for %s: %s", concern.get("id"), exc)
            text = ""
        responses.append({"concern_id": concern.get("id", ""), "response": text})
    return {"responses": responses}


def coverage_audit(concerns: list[dict], responses: list[dict]) -> dict:
    """Confirm every concern is addressed; flag the unaddressed ones."""
    concern_ids = [c.get("id") for c in concerns if c.get("id")]
    answered = {r.get("concern_id") for r in responses
                if r.get("concern_id") and (r.get("response") or "").strip()}
    covered = [cid for cid in concern_ids if cid in answered]
    gaps = [cid for cid in concern_ids if cid not in answered]
    total = len(concern_ids)
    pct = (len(covered) / total) if total else 1.0
    return {"covered": covered, "gaps": gaps, "coverage_pct": pct}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/services/skills/rebuttal-response/test_rebuttal.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/skills/rebuttal-response/__init__.py services/skills/rebuttal-response/rebuttal.py tests/services/skills/rebuttal-response/
git commit -m "feat(skills): add rebuttal-response logic module (B2)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 2: Add SKILL.md, server.py, and requirements.txt

**Files:**
- Create: `services/skills/rebuttal-response/SKILL.md`
- Create: `services/skills/rebuttal-response/server.py`
- Create: `services/skills/rebuttal-response/requirements.txt`

- [ ] **Step 1: Write the failing test**

Append to `tests/services/skills/rebuttal-response/test_rebuttal.py`:

```python
def test_skill_md_frontmatter_parses():
    import frontmatter
    skill_md = _MODULE_PATH.parent / "SKILL.md"
    assert skill_md.exists()
    meta, _ = frontmatter.parse(skill_md.read_text(encoding="utf-8"))
    assert meta["name"] == "rebuttal-response"
    assert meta["requires"] == ["pdf-parse", "paper-rag", "citation-check"]


def test_server_lists_three_tools():
    import importlib.util, asyncio
    server_path = _MODULE_PATH.parent / "server.py"
    spec = importlib.util.spec_from_file_location("rebuttal_server", server_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tools = asyncio.get_event_loop().run_until_complete(mod.list_tools())
    assert {t.name for t in tools} == {"parse_reviews", "draft_response", "coverage_audit"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/skills/rebuttal-response/test_rebuttal.py::test_skill_md_frontmatter_parses tests/services/skills/rebuttal-response/test_rebuttal.py::test_server_lists_three_tools -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

Create `services/skills/rebuttal-response/SKILL.md` (frontmatter pasted EXACTLY):

```markdown
---
name: rebuttal-response
description: >-
  Builds a structured author response to peer-review comments. parse_reviews
  decomposes reviewer text into an itemized concern matrix (severity, type,
  target section); draft_response generates point-by-point replies grounded in
  the paper via paper-rag and validated by citation-check; coverage_audit
  confirms every reviewer point is addressed and flags unaddressed ones. Use when
  responding to reviewer comments, writing an ARR or conference rebuttal, or
  planning a revision from a decision letter. Requires the paper (via pdf-parse)
  and the review text. Pairs with critique for self-review of the drafted response.
version: "0.1.0"
license: MIT
requires: ["pdf-parse", "paper-rag", "citation-check"]
---

# rebuttal-response Skill

Turns reviewer comments into a structured, audited author response.

## When to use

- Responding to reviewer comments for a conference or ARR rebuttal.
- Planning a revision from a decision letter.

## Tools

- `parse_reviews(review_text)` — `{concerns: [{id, severity, type, target_section, text}]}`.
- `draft_response(concerns, paper_context)` — `{responses: [{concern_id, response}]}`
  (one Gemma call per concern).
- `coverage_audit(concerns, responses)` — `{covered, gaps, coverage_pct}`.

## Constraints

- Replies must be grounded in the provided paper context; never invent results.
- Pair with `critique` to self-review the drafted response before sending.
```

Create `services/skills/rebuttal-response/server.py`:

```python
"""MCP server for the rebuttal-response skill (stdio JSON-RPC).

CRITICAL: stdout carries JSON-RPC. All logging goes to stderr.
"""
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
import asyncio
import json
import logging
import sys

import rebuttal

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("rebuttal-response.server")
app: Server = Server("rebuttal-response")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="parse_reviews",
             description="Decompose reviewer text into an itemized concern matrix.",
             inputSchema={"type": "object",
                          "properties": {"review_text": {"type": "string"}},
                          "required": ["review_text"]}),
        Tool(name="draft_response",
             description="Generate point-by-point replies grounded in the paper.",
             inputSchema={"type": "object", "properties": {
                 "concerns": {"type": "array", "items": {"type": "object"}},
                 "paper_context": {"type": "string"}},
                 "required": ["concerns", "paper_context"]}),
        Tool(name="coverage_audit",
             description="Confirm every concern is addressed; flag the gaps.",
             inputSchema={"type": "object", "properties": {
                 "concerns": {"type": "array", "items": {"type": "object"}},
                 "responses": {"type": "array", "items": {"type": "object"}}},
                 "required": ["concerns", "responses"]}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "parse_reviews":
            result = rebuttal.parse_reviews(arguments["review_text"])
        elif name == "draft_response":
            result = await rebuttal.draft_response(
                arguments["concerns"], arguments["paper_context"])
        elif name == "coverage_audit":
            result = rebuttal.coverage_audit(
                arguments["concerns"], arguments["responses"])
        else:
            raise ValueError(f"unknown tool: {name}")
        return [TextContent(type="text", text=json.dumps(result, default=str))]
    except Exception as exc:
        log.exception("tool %s failed", name)
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]


async def main() -> None:
    async with stdio_server() as (r, w):
        await app.run(r, w, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
```

Create `services/skills/rebuttal-response/requirements.txt`:

```
mcp>=1.0.0
litellm>=1.0.0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/services/skills/rebuttal-response/test_rebuttal.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/skills/rebuttal-response/SKILL.md services/skills/rebuttal-response/server.py services/skills/rebuttal-response/requirements.txt
git commit -m "feat(skills): add rebuttal-response SKILL.md + MCP server (B2)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 3: Verify catalog registration

**Files:**
- Modify: `tests/services/skills/rebuttal-response/test_rebuttal.py`

Discovery is automatic from SKILL.md frontmatter. This guard test proves `SkillRunner` catalogs the skill and `catalog_prompt()` advertises it.

- [ ] **Step 1: Write the failing test**

Append to `tests/services/skills/rebuttal-response/test_rebuttal.py`:

```python
def test_skill_runner_catalogs_rebuttal_response():
    from services.skill_runner.skill_runner import SkillRunner
    skills_root = _MODULE_PATH.resolve().parent.parent  # .../services/skills
    runner = SkillRunner(roots=[skills_root])
    runner.discover()
    assert "rebuttal-response" in runner.catalog
    prompt = runner.catalog_prompt()
    assert "rebuttal-response" in prompt
    assert "rebuttal" in prompt.lower()
```

- [ ] **Step 2: Run test to verify it passes**

Run: `pytest tests/services/skills/rebuttal-response/test_rebuttal.py::test_skill_runner_catalogs_rebuttal_response -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/services/skills/rebuttal-response/test_rebuttal.py
git commit -m "test(skills): assert rebuttal-response is cataloged (B2)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```
