# paper-to-slides MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the paper-to-slides Python MCP server — a 5-stage pipeline (outline → optional figure triage → Beamer generation → compile-fix loop → optional speaker notes) that converts a parsed scientific paper into a conference presentation PDF.

**Architecture:** OutlinePlanner maps paper sections to IMRaD slide blueprint (JSON); SlideGenerator emits Beamer .tex or Marp .md from the blueprint; CompileLoop shells out to tectonic, captures compile errors, calls Gemma 4 to repair the .tex, and retries up to 5 times; FigureTriage uses Gemma 4 vision to score/describe each figure for slide use (optional). All LLM calls via litellm to GEMMA_BASE. All logging to stderr.

**Tech Stack:** Python 3.11+, `mcp` SDK, `litellm`, `pydantic>=2`, `tectonic` (system binary), `pytest`, `pytest-asyncio`

---

## Critical constraints (apply to every task)

- **stdout is sacred.** All logging uses `logging` configured with `stream=sys.stderr`. NEVER `print()`. stdout carries JSON-RPC 2.0 framing; any stray byte corrupts the stream silently. The MCP server's `logging.basicConfig(stream=sys.stderr, ...)` MUST run before importing any pipeline module.
- **Never tiktoken.** If token counting is ever needed, use `AutoTokenizer.from_pretrained("google/gemma-4-9b-it")`. Gemma uses SentencePiece.
- **Single-GPU.** Every LLM call (text AND vision) goes to `GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")` with model `GEMMA_MODEL = os.getenv("GEMMA_MODEL", "google/gemma-4-31B-it")`. There is NO Qwen, NO QWEN_BASE anywhere in this skill. Gemma 4 31B does the outline reasoning, the .tex repair, the figure-vision triage, and the speaker notes.
- **Requires the `pdf-parse` skill.** `parsed_paper_path` is the JSON output of `pdf-parse` (a single `parse` result object). Its contract is: `{"markdown": str, "figures": [{"path", "caption", "page"}], "tables": [{"html", "caption", "page"}], "metadata": {"title", "authors", "doi", "page_count"}}`. Figure `path` values are absolute PNG paths under the pdf-parse output dir.
- **tectonic over pdflatex.** Use the `tectonic` binary (self-contained, no system TeX install). If `shutil.which("tectonic")` is None, fall back to `pdflatex`. If neither exists, return `success=False` with an actionable `final_error`.
- **Child process over stdio.** The server is spawned by the SkillRegistry over stdio. No TTY, no banners, write outputs only under the configured output dir.

---

## Pipeline overview

```
pdf-parse output (markdown + figures[] + tables[] + metadata)
  → [1] OutlinePlanner   — IMRaD → PresentationBlueprint (JSON)
  → [2] FigureTriage     — OPTIONAL: Gemma 4 vision scores each figure PNG
  → [3] SlideGenerator   — blueprint → slides.tex (Beamer) or slides.md (Marp)
  → [4] CompileLoop      — tectonic compile → log → Gemma 4 repair → recompile (≤5)
  → [5] SpeakerNotes     — OPTIONAL: per-slide talk track timed to duration
OUTPUT: slides.tex + slides.pdf (+ optional notes.md, + optional slides.md for Marp)
```

**Tools exposed (3):**
- `paper_to_slides.generate(parsed_paper_path, talk_duration_min=20, output_format="beamer", include_notes=False)` — full pipeline.
- `paper_to_slides.generate_outline(parsed_paper_path, talk_duration_min=20)` — stage [1] only; returns the JSON blueprint for review/editing.
- `paper_to_slides.compile_tex(tex_path, max_retries=5)` — stage [4] only; compile + self-correct an existing .tex.

---

## Phase 0 — Scaffolding

### Task 0.1 — Create directory structure

- [ ] Create the skill server, templates, and test directories.

```bash
mkdir -p /Users/zachstallbohm/Work/gemma/services/skills/paper-to-slides/templates
mkdir -p /Users/zachstallbohm/Work/gemma/tests/services/skills/paper-to-slides/fixtures
```

### Task 0.2 — Write requirements.txt

- [ ] Create `services/skills/paper-to-slides/requirements.txt`:

```text
mcp>=1.0.0
litellm>=1.40.0
pydantic>=2.0.0
```

> `tectonic` is a system binary, not a pip package — it is documented in SKILL.md, not requirements.txt. `transformers` is only needed if token counting is added later; omit it for now.

### Task 0.3 — Write SKILL.md

- [ ] Create `services/skills/paper-to-slides/SKILL.md` (frontmatter exactly as below; body is model-agnostic, no absolute paths):

```markdown
---
name: paper-to-slides
description: >
  Converts a scientific paper (pdf-parse output) into a conference presentation.
  Generates a structured IMRaD slide outline, then emits LaTeX Beamer code compiled
  to PDF via tectonic with automatic self-correction. Optional: Gemma 4 vision
  figure triage, speaker notes, Marp Markdown alternative output.
  Use when preparing a conference talk from an accepted paper.
trigger: "Use when creating conference presentation slides from a scientific paper"
tools:
  - paper_to_slides.generate
  - paper_to_slides.generate_outline
  - paper_to_slides.compile_tex
version: "0.1.0"
license: MIT
requires: [pdf-parse]
---

# Paper to Slides Skill

You have access to the `paper_to_slides` MCP server, which turns a parsed
scientific paper into a conference talk: a LaTeX Beamer deck compiled to PDF
(primary), or a Marp Markdown deck (secondary).

## When to Use

- Preparing a conference talk from an accepted/published paper
- Generating a reviewable slide outline before committing to a full deck
- Repairing and recompiling a hand-edited `.tex` deck

## Prerequisite

Run the `pdf-parse` skill first. Feed its JSON `parse` result (saved to a file)
as `parsed_paper_path`.

## Available Tools

### `paper_to_slides.generate`

Full pipeline. Returns JSON: `tex_path`, `pdf_path`, `notes_path`,
`slide_count`, `compile_success`.

```json
{ "parsed_paper_path": "/work/attention.json", "talk_duration_min": 20,
  "output_format": "beamer", "include_notes": false }
```

### `paper_to_slides.generate_outline`

Outline only. Returns the JSON PresentationBlueprint for review/editing.

```json
{ "parsed_paper_path": "/work/attention.json", "talk_duration_min": 20 }
```

### `paper_to_slides.compile_tex`

Compile + self-correct an existing `.tex`. Returns JSON: `pdf_path`,
`success`, `attempts`, `final_error`.

```json
{ "tex_path": "/work/slides.tex", "max_retries": 5 }
```

## Requirements

- `tectonic` on PATH (preferred). Falls back to `pdflatex` if tectonic is
  absent. If neither is installed, compilation returns `success: false`.

## Output Contract

`generate` returns:

\```json
{ "tex_path": "...", "pdf_path": "...", "notes_path": null,
  "slide_count": 13, "compile_success": true }
\```
```

---

## Phase 1 — Shared types and LLM helper

### Task 1.1 — Define blueprint dataclasses in `outline_planner.py`

- [ ] Create `services/skills/paper-to-slides/outline_planner.py` with the shared types and IMRaD budget. (LLM logic added in Task 1.3.)

```python
"""OutlinePlanner: pdf-parse JSON -> PresentationBlueprint (IMRaD slide plan)."""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field

import litellm

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("paper-to-slides.outline")

GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")
GEMMA_MODEL = os.getenv("GEMMA_MODEL", "google/gemma-4-31B-it")


@dataclass
class SlideBlueprint:
    index: int
    title: str
    section: str          # 'title'|'intro'|'methods'|'results'|'discussion'|'conclusion'|'refs'
    bullets: list[str] = field(default_factory=list)
    figure_paths: list[str] = field(default_factory=list)   # absolute paths from pdf-parse
    table_html: str | None = None
    speaker_note_hint: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PresentationBlueprint:
    paper_title: str
    authors: list[str]
    venue: str
    talk_duration_min: int
    target_slide_count: int
    slides: list[SlideBlueprint] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "paper_title": self.paper_title,
            "authors": self.authors,
            "venue": self.venue,
            "talk_duration_min": self.talk_duration_min,
            "target_slide_count": self.target_slide_count,
            "slides": [s.to_dict() for s in self.slides],
        }
```

### Task 1.2 — Add the IMRaD slide budget constant

- [ ] In `outline_planner.py`, add the `OutlinePlanner` class shell with the IMRaD budget and slide-count rule.

```python
class OutlinePlanner:
    # 13 slides for a 20-min talk; scaled proportionally for other durations.
    IMRAD_SLIDE_BUDGET = {
        "title": 1,
        "outline": 1,        # skipped for talks < 15 min
        "intro": 2,
        "methods": 3,
        "results": 3,
        "discussion": 1,
        "conclusion": 1,
        "refs": 1,
    }

    def __init__(self) -> None:
        pass

    @staticmethod
    def _target_slide_count(talk_duration_min: int) -> int:
        # Rule: ~1 slide per 2 minutes, floor of 6.
        return max(6, int(talk_duration_min / 2))

    def _scaled_budget(self, talk_duration_min: int) -> dict[str, int]:
        """Scale the 13-slide base budget to the target count; drop 'outline' < 15 min."""
        budget = dict(self.IMRAD_SLIDE_BUDGET)
        if talk_duration_min < 15:
            budget.pop("outline", None)
        base_total = sum(budget.values())
        target = self._target_slide_count(talk_duration_min)
        scale = target / base_total
        scaled = {k: max(1, round(v * scale)) for k, v in budget.items()}
        log.info("scaled budget for %d min: %s (target=%d)",
                 talk_duration_min, scaled, target)
        return scaled
```

### Task 1.3 — Add the LLM helper and prompt to `OutlinePlanner`

- [ ] Add `_call_llm` (litellm → GEMMA_BASE) and the planning prompt builder. Mirrors the canonical pattern in `citation-check/claim_verifier.py`.

```python
    PLAN_PROMPT = """You are a conference-talk planner. Build a slide outline for a \
{duration}-minute talk from the paper below. Produce EXACTLY these per-section slide \
counts: {budget}. Map paper content to IMRaD sections.

Return ONLY a JSON object with this shape:
{{"paper_title": str, "authors": [str], "venue": str,
  "slides": [{{"index": int, "title": str, "section": str,
              "bullets": [str], "figure_paths": [str], "table_html": str|null,
              "speaker_note_hint": str}}]}}

Use only figure paths drawn from AVAILABLE FIGURES. Keep bullets terse (<= 12 words).

PAPER METADATA: {metadata}
AVAILABLE FIGURES: {figures}
PAPER MARKDOWN (truncated):
{markdown}
"""

    def _call_llm(self, prompt: str) -> str:
        resp = litellm.completion(
            model=f"openai/{GEMMA_MODEL}",
            api_base=GEMMA_BASE,
            api_key="not-needed",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        return resp["choices"][0]["message"]["content"]
```

> Truncate `markdown` to a safe budget (e.g. first ~24k chars) before formatting the prompt so the context window is not exceeded. Do NOT use tiktoken; a char cap is sufficient here.

---

## Phase 2 — OutlinePlanner.plan

### Task 2.1 — Implement `_parse_blueprint_json`

- [ ] Tolerant JSON parse that returns a `PresentationBlueprint`. Strips code fences, locates the outermost object.

```python
    def _parse_blueprint_json(self, raw: str) -> PresentationBlueprint:
        s = raw.strip()
        if s.startswith("```"):
            s = s.split("```", 2)[1]
            if s.startswith("json"):
                s = s[4:]
        start, end = s.find("{"), s.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("no JSON object found in outline output")
        data = json.loads(s[start : end + 1])
        slides = [
            SlideBlueprint(
                index=int(sl["index"]),
                title=sl["title"],
                section=sl["section"],
                bullets=sl.get("bullets", []),
                figure_paths=sl.get("figure_paths", []),
                table_html=sl.get("table_html"),
                speaker_note_hint=sl.get("speaker_note_hint", ""),
            )
            for sl in data.get("slides", [])
        ]
        return PresentationBlueprint(
            paper_title=data.get("paper_title", ""),
            authors=data.get("authors", []),
            venue=data.get("venue", ""),
            talk_duration_min=0,   # filled by caller in plan()
            target_slide_count=len(slides),
            slides=slides,
        )
```

### Task 2.2 — Implement `plan`

- [ ] Wire metadata extraction, prompt build, LLM call, parse, and budget/duration fill-in.

```python
    def plan(self, parsed_paper: dict, talk_duration_min: int = 20) -> PresentationBlueprint:
        meta = parsed_paper.get("metadata", {}) or {}
        figures = parsed_paper.get("figures", []) or []
        budget = self._scaled_budget(talk_duration_min)
        prompt = self.PLAN_PROMPT.format(
            duration=talk_duration_min,
            budget=json.dumps(budget),
            metadata=json.dumps({
                "title": meta.get("title", ""),
                "authors": meta.get("authors", []),
                "venue": meta.get("venue", meta.get("doi", "")),
            }),
            figures=json.dumps(
                [{"path": f["path"], "caption": f.get("caption", "")} for f in figures]
            ),
            markdown=(parsed_paper.get("markdown", "") or "")[:24000],
        )
        blueprint = self._parse_blueprint_json(self._call_llm(prompt))
        blueprint.talk_duration_min = talk_duration_min
        blueprint.target_slide_count = self._target_slide_count(talk_duration_min)
        log.info("planned %d slides for %d-min talk",
                 len(blueprint.slides), talk_duration_min)
        return blueprint
```

---

## Phase 3 — FigureTriage (optional, Gemma 4 vision)

### Task 3.1 — Create `figure_triage.py` with base64 image helper

- [ ] Create `services/skills/paper-to-slides/figure_triage.py`. Reads each figure PNG, base64-encodes it, sends it to Gemma 4 as a vision message.

```python
"""FigureTriage: Gemma 4 vision scoring/description of figure PNGs (optional)."""
from __future__ import annotations

import base64
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import litellm

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("paper-to-slides.figtriage")

GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")
GEMMA_MODEL = os.getenv("GEMMA_MODEL", "google/gemma-4-31B-it")


@dataclass
class FigureScore:
    path: str
    slide_worthy: bool
    score: float           # 0.0 - 1.0
    description: str

    def to_dict(self) -> dict:
        return {"path": self.path, "slide_worthy": self.slide_worthy,
                "score": self.score, "description": self.description}


def _encode_png(path: str) -> str:
    data = Path(path).read_bytes()
    return base64.b64encode(data).decode("ascii")
```

### Task 3.2 — Implement `FigureTriage.score_figure`

- [ ] Single-figure vision call. Same GEMMA_BASE; the message carries an `image_url` with a `data:` URI.

```python
class FigureTriage:
    VISION_PROMPT = (
        "Score this scientific figure for use on a single conference slide. "
        "Return ONLY JSON: {\"slide_worthy\": bool, \"score\": float, "
        "\"description\": str}. score is 0..1 readability-at-distance. "
        "Caption: {caption}"
    )

    def score_figure(self, path: str, caption: str = "") -> FigureScore:
        b64 = _encode_png(path)
        resp = litellm.completion(
            model=f"openai/{GEMMA_MODEL}",
            api_base=GEMMA_BASE,
            api_key="not-needed",
            temperature=0.0,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text",
                     "text": self.VISION_PROMPT.format(caption=caption)},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            }],
        )
        raw = resp["choices"][0]["message"]["content"].strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
        start, end = raw.find("{"), raw.rfind("}")
        data = json.loads(raw[start : end + 1])
        return FigureScore(
            path=path,
            slide_worthy=bool(data.get("slide_worthy", False)),
            score=float(data.get("score", 0.0)),
            description=data.get("description", ""),
        )
```

### Task 3.3 — Implement `FigureTriage.triage`

- [ ] Score a list of figures; never let one bad image abort the batch.

```python
    def triage(self, figures: list[dict]) -> list[FigureScore]:
        scored: list[FigureScore] = []
        for f in figures:
            try:
                scored.append(self.score_figure(f["path"], f.get("caption", "")))
            except Exception as exc:  # one bad figure must not abort triage
                log.warning("figure triage failed for %s: %s", f.get("path"), exc)
                scored.append(FigureScore(f["path"], False, 0.0, ""))
        return scored
```

---

## Phase 4 — SlideGenerator (Beamer + Marp)

### Task 4.1 — Write the Beamer preamble template

- [ ] Create `services/skills/paper-to-slides/templates/beamer_preamble.tex`:

```latex
\documentclass[aspectratio=169]{beamer}
\usetheme{Madrid}
\usepackage{graphicx}
\usepackage{booktabs}
\usepackage[utf8]{inputenc}
\usepackage{hyperref}
\title{%(title)s}
\author{%(authors)s}
\institute{%(venue)s}
\date{}
```

> `%(...)s` placeholders are filled with Python `str %` formatting. Keep the file ASCII; the LLM repair loop handles any unicode issues that surface at compile time.

### Task 4.2 — Write the Marp header template

- [ ] Create `services/skills/paper-to-slides/templates/marp_header.md`:

```markdown
---
marp: true
theme: default
paginate: true
title: %(title)s
---

# %(title)s

%(authors)s
%(venue)s
```

### Task 4.3 — Create `slide_generator.py` with the Beamer emitter

- [ ] Create `services/skills/paper-to-slides/slide_generator.py`. Pure templating (no LLM) — deterministic and testable.

```python
"""SlideGenerator: PresentationBlueprint -> Beamer .tex or Marp .md."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from outline_planner import PresentationBlueprint, SlideBlueprint

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("paper-to-slides.slidegen")

_TPL_DIR = Path(__file__).parent / "templates"


def _latex_escape(s: str) -> str:
    repl = {"&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
            "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
            "^": r"\textasciicircum{}"}
    return "".join(repl.get(c, c) for c in s)


class SlideGenerator:
    def to_beamer(self, bp: PresentationBlueprint) -> str:
        preamble = (_TPL_DIR / "beamer_preamble.tex").read_text() % {
            "title": _latex_escape(bp.paper_title),
            "authors": _latex_escape(", ".join(bp.authors)),
            "venue": _latex_escape(bp.venue),
        }
        body = ["\\begin{document}", "\\frame{\\titlepage}"]
        for sl in bp.slides:
            if sl.section == "title":
                continue
            body.append(self._beamer_frame(sl))
        body.append("\\end{document}")
        return preamble + "\n" + "\n".join(body) + "\n"

    def _beamer_frame(self, sl: SlideBlueprint) -> str:
        lines = [f"\\begin{{frame}}{{{_latex_escape(sl.title)}}}"]
        if sl.bullets:
            lines.append("\\begin{itemize}")
            lines += [f"  \\item {_latex_escape(b)}" for b in sl.bullets]
            lines.append("\\end{itemize}")
        for fp in sl.figure_paths:
            lines.append(
                f"\\begin{{center}}\\includegraphics[width=0.8\\textwidth]{{{fp}}}"
                f"\\end{{center}}"
            )
        lines.append("\\end{frame}")
        return "\n".join(lines)
```

### Task 4.4 — Add the Marp emitter

- [ ] Add `to_marp` to `SlideGenerator`. Slides separated by `---`; figures as Markdown images.

```python
    def to_marp(self, bp: PresentationBlueprint) -> str:
        header = (_TPL_DIR / "marp_header.md").read_text() % {
            "title": bp.paper_title,
            "authors": ", ".join(bp.authors),
            "venue": bp.venue,
        }
        out = [header]
        for sl in bp.slides:
            if sl.section == "title":
                continue
            out.append("---\n")
            out.append(f"## {sl.title}\n")
            for b in sl.bullets:
                out.append(f"- {b}")
            for fp in sl.figure_paths:
                out.append(f"\n![w:800]({fp})")
            out.append("")
        return "\n".join(out) + "\n"
```

### Task 4.5 — Add `generate` dispatcher that writes the file

- [ ] Add a single entry that selects format, writes the file, returns the path.

```python
    def generate(self, bp: PresentationBlueprint, out_dir: str,
                 output_format: str = "beamer") -> str:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        if output_format == "marp":
            path = out / "slides.md"
            path.write_text(self.to_marp(bp))
        else:
            path = out / "slides.tex"
            path.write_text(self.to_beamer(bp))
        log.info("wrote %s (%d slides)", path, len(bp.slides))
        return str(path)
```

---

## Phase 5 — CompileLoop (tectonic + LLM self-correction)

### Task 5.1 — Create `compile_loop.py` with `CompileResult` and runner

- [ ] Create `services/skills/paper-to-slides/compile_loop.py`. `_run_tectonic` prefers tectonic, falls back to pdflatex.

```python
"""CompileLoop: tectonic compile + Gemma 4 .tex self-correction (max 5 retries)."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import litellm

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("paper-to-slides.compile")

GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")
GEMMA_MODEL = os.getenv("GEMMA_MODEL", "google/gemma-4-31B-it")


@dataclass
class CompileResult:
    success: bool
    pdf_path: str | None
    attempts: int
    final_error: str | None

    def to_dict(self) -> dict:
        return {"success": self.success, "pdf_path": self.pdf_path,
                "attempts": self.attempts, "final_error": self.final_error}


class CompileLoop:
    MAX_RETRIES: int = 5

    def _run_tectonic(self, tex_path: str) -> tuple[bool, str]:
        """Compile tex_path. Prefer tectonic; fall back to pdflatex. Returns (ok, log)."""
        path = Path(tex_path)
        if shutil.which("tectonic"):
            cmd = ["tectonic", "--keep-logs", "--outdir", str(path.parent), str(path)]
        elif shutil.which("pdflatex"):
            cmd = ["pdflatex", "-interaction=nonstopmode",
                   "-output-directory", str(path.parent), str(path)]
        else:
            return False, "neither 'tectonic' nor 'pdflatex' found on PATH"
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return False, "compile timed out after 120s"
        ok = proc.returncode == 0
        return ok, (proc.stdout + "\n" + proc.stderr)
```

### Task 5.2 — Implement `_repair_tex`

- [ ] LLM repair call: feed current .tex + error log, get a corrected full .tex back.

```python
    REPAIR_PROMPT = """The following LaTeX Beamer document failed to compile. \
Fix the errors and return ONLY the complete corrected .tex source (no commentary, \
no code fences).

COMPILE LOG (tail):
{log}

CURRENT .tex:
{tex}
"""

    def _repair_tex(self, tex: str, error_log: str) -> str:
        resp = litellm.completion(
            model=f"openai/{GEMMA_MODEL}",
            api_base=GEMMA_BASE,
            api_key="not-needed",
            temperature=0.0,
            messages=[{"role": "user", "content": self.REPAIR_PROMPT.format(
                log=error_log[-4000:], tex=tex)}],
        )
        out = resp["choices"][0]["message"]["content"].strip()
        if out.startswith("```"):
            out = out.split("```", 2)[1]
            if out.startswith("latex"):
                out = out[5:]
            elif out.startswith("tex"):
                out = out[3:]
        return out.strip()
```

### Task 5.3 — Implement `compile` with the retry loop

- [ ] Compile once; on failure, repair the .tex in place and retry up to `max_retries`. Marp `.md` is not compiled here (handled by the server: Marp output skips this loop).

```python
    def compile(self, tex_path: str, max_retries: int = 5) -> CompileResult:
        path = Path(tex_path)
        pdf_path = path.with_suffix(".pdf")
        last_log = ""
        for attempt in range(1, max_retries + 1):
            ok, last_log = self._run_tectonic(str(path))
            if ok and pdf_path.exists():
                log.info("compile succeeded on attempt %d", attempt)
                return CompileResult(True, str(pdf_path), attempt, None)
            log.warning("compile attempt %d failed; repairing", attempt)
            if attempt < max_retries:
                repaired = self._repair_tex(path.read_text(), last_log)
                path.write_text(repaired)
        return CompileResult(False, None, max_retries, last_log[-2000:])
```

> The loop calls `_repair_tex` only when there is another attempt left, so the final failing attempt does not waste an LLM call. Tests assert: success path returns early; repair is invoked on error; exhaustion returns `success=False` with `attempts == max_retries`.

---

## Phase 6 — SpeakerNotes (optional)

### Task 6.1 — Create `speaker_notes.py`

- [ ] Create `services/skills/paper-to-slides/speaker_notes.py`. One talk-track paragraph per slide, budgeted to the talk duration.

```python
"""SpeakerNotes: per-slide talk track timed to the talk duration (optional)."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import litellm

from outline_planner import PresentationBlueprint

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("paper-to-slides.notes")

GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")
GEMMA_MODEL = os.getenv("GEMMA_MODEL", "google/gemma-4-31B-it")


class SpeakerNotes:
    def generate(self, bp: PresentationBlueprint, out_dir: str) -> str:
        per_slide_sec = int(bp.talk_duration_min * 60 / max(1, len(bp.slides)))
        sections = []
        for sl in bp.slides:
            prompt = (
                f"Write a {per_slide_sec}-second spoken talk track for this slide. "
                f"Plain prose, first person, no markdown.\n"
                f"TITLE: {sl.title}\nBULLETS: {sl.bullets}\nHINT: {sl.speaker_note_hint}"
            )
            resp = litellm.completion(
                model=f"openai/{GEMMA_MODEL}",
                api_base=GEMMA_BASE,
                api_key="not-needed",
                temperature=0.3,
                messages=[{"role": "user", "content": prompt}],
            )
            note = resp["choices"][0]["message"]["content"].strip()
            sections.append(f"## Slide {sl.index}: {sl.title}\n\n{note}\n")
        path = Path(out_dir) / "notes.md"
        path.write_text("\n".join(sections) + "\n")
        log.info("wrote speaker notes: %s", path)
        return str(path)
```

---

## Phase 7 — MCP server (the 3 tools)

### Task 7.1 — Create `server.py` with logging + tool listing

- [ ] Create `services/skills/paper-to-slides/server.py`. Configure stderr logging BEFORE importing pipeline modules.

```python
"""MCP stdio server for the paper_to_slides skill.

CRITICAL: stdout is the JSON-RPC 2.0 channel. NEVER print() anywhere.
All logging is configured to sys.stderr before pipeline imports.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("paper-to-slides.server")

from compile_loop import CompileLoop          # noqa: E402
from outline_planner import OutlinePlanner     # noqa: E402
from slide_generator import SlideGenerator     # noqa: E402
from speaker_notes import SpeakerNotes         # noqa: E402

app: Server = Server("paper-to-slides")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="generate",
            description="Full pipeline: parsed paper -> Beamer/Marp deck + PDF. "
                        "Returns JSON with tex_path, pdf_path, notes_path, "
                        "slide_count, compile_success.",
            inputSchema={
                "type": "object",
                "properties": {
                    "parsed_paper_path": {"type": "string",
                        "description": "Absolute path to pdf-parse JSON output."},
                    "talk_duration_min": {"type": "integer", "default": 20},
                    "output_format": {"type": "string",
                        "enum": ["beamer", "marp"], "default": "beamer"},
                    "include_notes": {"type": "boolean", "default": False},
                },
                "required": ["parsed_paper_path"],
            },
        ),
        Tool(
            name="generate_outline",
            description="Outline only. Returns the JSON PresentationBlueprint.",
            inputSchema={
                "type": "object",
                "properties": {
                    "parsed_paper_path": {"type": "string"},
                    "talk_duration_min": {"type": "integer", "default": 20},
                },
                "required": ["parsed_paper_path"],
            },
        ),
        Tool(
            name="compile_tex",
            description="Compile + self-correct an existing .tex. Returns JSON with "
                        "pdf_path, success, attempts, final_error.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tex_path": {"type": "string"},
                    "max_retries": {"type": "integer", "default": 5},
                },
                "required": ["tex_path"],
            },
        ),
    ]
```

### Task 7.2 — Implement `call_tool` dispatch

- [ ] Add the `@app.call_tool()` handler wiring the three tools to the pipeline. Output dir derives from the parsed-paper file's directory.

```python
def _output_dir_for(parsed_paper_path: str) -> str:
    base = os.getenv("PAPER_TO_SLIDES_OUTPUT_DIR")
    if base:
        return base
    return os.path.join(os.path.dirname(os.path.abspath(parsed_paper_path)), "slides")


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "generate_outline":
            paper = json.loads(open(arguments["parsed_paper_path"]).read())
            bp = OutlinePlanner().plan(
                paper, arguments.get("talk_duration_min", 20))
            text = json.dumps(bp.to_dict(), ensure_ascii=False)

        elif name == "compile_tex":
            res = CompileLoop().compile(
                arguments["tex_path"], arguments.get("max_retries", 5))
            text = json.dumps(res.to_dict(), ensure_ascii=False)

        elif name == "generate":
            text = _run_generate(arguments)

        else:
            log.error("unknown tool: %s", name)
            return [TextContent(type="text", text=f"unknown tool: {name}")]
    except Exception as exc:  # never crash the stdio server
        log.exception("tool %s failed", name)
        return [TextContent(type="text", text=f"error: {exc!r}")]

    return [TextContent(type="text", text=text)]
```

### Task 7.3 — Implement `_run_generate` (the full ordered pipeline)

- [ ] Add the helper that runs stages [1]→[3]→[4]→([5]) in order. (FigureTriage [2] is invoked inside outline post-processing only when enabled via env; default off to save GPU.) Returns the JSON output contract.

```python
def _run_generate(args: dict) -> str:
    parsed_path = args["parsed_paper_path"]
    duration = args.get("talk_duration_min", 20)
    output_format = args.get("output_format", "beamer")
    include_notes = args.get("include_notes", False)

    paper = json.loads(open(parsed_path).read())
    out_dir = _output_dir_for(parsed_path)

    # [1] outline
    bp = OutlinePlanner().plan(paper, duration)
    # [3] slide source
    src_path = SlideGenerator().generate(bp, out_dir, output_format)

    # [4] compile (Beamer only; Marp is not compiled to PDF here)
    if output_format == "beamer":
        result = CompileLoop().compile(src_path)
        pdf_path = result.pdf_path
        compile_success = result.success
    else:
        pdf_path = None
        compile_success = True  # Marp .md needs no LaTeX compile

    # [5] optional speaker notes
    notes_path = None
    if include_notes:
        notes_path = SpeakerNotes().generate(bp, out_dir)

    return json.dumps({
        "tex_path": src_path,
        "pdf_path": pdf_path,
        "notes_path": notes_path,
        "slide_count": len(bp.slides),
        "compile_success": compile_success,
    }, ensure_ascii=False)


async def main() -> None:
    log.info("starting paper-to-slides MCP server")
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
```

> `tex_path` in the output contract holds the slide source path regardless of format — for Marp it is the `slides.md` path. FigureTriage stays opt-in: a later task can gate it behind `PAPER_TO_SLIDES_TRIAGE=1` and merge top-scored figures into the blueprint before generation. The default pipeline does not call vision, conserving the single GPU.

---

## Phase 8 — Tests (all `@pytest.mark.mocked`)

### Task 8.1 — `conftest.py` with fixtures

- [ ] Create `tests/services/skills/paper-to-slides/conftest.py`. Add the skill dir to `sys.path`; provide a sample parsed-paper dict and a fake litellm response factory.

```python
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[4] / "services" / "skills" / "paper-to-slides"
sys.path.insert(0, str(SKILL))


@pytest.fixture
def parsed_paper():
    return {
        "markdown": "# Intro\nWe study X.\n# Methods\nWe do Y.\n# Results\nZ works.",
        "figures": [{"path": "/tmp/fig1.png", "caption": "Architecture", "page": 2}],
        "tables": [{"html": "<table></table>", "caption": "Scores", "page": 4}],
        "metadata": {"title": "On X", "authors": ["A. Author"], "doi": "10.0/x",
                     "page_count": 8},
    }


def _llm_json(obj):
    import json
    return {"choices": [{"message": {"content": json.dumps(obj)}}]}


@pytest.fixture
def llm_json():
    return _llm_json
```

### Task 8.2 — `test_outline_planner.py`

- [ ] Mock `litellm.completion`; assert slide count and IMRaD mapping for a 20-min talk.

```python
import pytest

from outline_planner import OutlinePlanner

pytestmark = pytest.mark.mocked


def _blueprint_obj(n):
    return {"paper_title": "On X", "authors": ["A. Author"], "venue": "NeurIPS",
            "slides": [{"index": i, "title": f"S{i}",
                        "section": ("methods" if i in (3, 4, 5) else "intro"),
                        "bullets": ["b"], "figure_paths": [], "table_html": None,
                        "speaker_note_hint": "h"} for i in range(1, n + 1)]}


def test_plan_returns_blueprint(monkeypatch, parsed_paper, llm_json):
    monkeypatch.setattr("outline_planner.litellm.completion",
                        lambda **k: llm_json(_blueprint_obj(13)))
    bp = OutlinePlanner().plan(parsed_paper, talk_duration_min=20)
    assert bp.target_slide_count == 10  # max(6, 20/2)
    assert len(bp.slides) == 13
    assert bp.talk_duration_min == 20


def test_methods_section_mapped(monkeypatch, parsed_paper, llm_json):
    monkeypatch.setattr("outline_planner.litellm.completion",
                        lambda **k: llm_json(_blueprint_obj(13)))
    bp = OutlinePlanner().plan(parsed_paper, talk_duration_min=20)
    assert any(s.section == "methods" for s in bp.slides)


def test_target_slide_count_rule():
    assert OutlinePlanner._target_slide_count(20) == 10
    assert OutlinePlanner._target_slide_count(4) == 6   # floor
```

### Task 8.3 — `test_slide_generator.py`

- [ ] Deterministic templating; no LLM. Assert Beamer frames and Marp separators.

```python
import pytest

from outline_planner import PresentationBlueprint, SlideBlueprint
from slide_generator import SlideGenerator

pytestmark = pytest.mark.mocked


def _bp():
    return PresentationBlueprint(
        paper_title="On X & Y", authors=["A"], venue="NeurIPS",
        talk_duration_min=20, target_slide_count=2,
        slides=[
            SlideBlueprint(1, "Title", "title"),
            SlideBlueprint(2, "Methods", "methods", bullets=["uses 50% data"],
                           figure_paths=["/tmp/fig1.png"]),
        ])


def test_to_beamer_has_frames_and_escapes():
    tex = SlideGenerator().to_beamer(_bp())
    assert "\\begin{document}" in tex and "\\frame{\\titlepage}" in tex
    assert "\\begin{frame}{Methods}" in tex
    assert r"\&" in tex and r"\%" in tex          # escaping
    assert "/tmp/fig1.png" in tex


def test_to_marp_has_separators():
    md = SlideGenerator().to_marp(_bp())
    assert "marp: true" in md and "---" in md
    assert "## Methods" in md and "![w:800](/tmp/fig1.png)" in md


def test_generate_writes_file(tmp_path):
    p = SlideGenerator().generate(_bp(), str(tmp_path), "beamer")
    assert p.endswith("slides.tex")
```

### Task 8.4 — `test_compile_loop.py`

- [ ] Mock `subprocess.run` and `litellm.completion`. Cover: success early-return, retry-on-error, exhaustion, tectonic→pdflatex fallback.

```python
import subprocess
import pytest

from compile_loop import CompileLoop

pytestmark = pytest.mark.mocked


class _Proc:
    def __init__(self, rc):
        self.returncode = rc
        self.stdout = "log"
        self.stderr = ""


def test_success_first_attempt(monkeypatch, tmp_path):
    tex = tmp_path / "slides.tex"; tex.write_text("x")
    (tmp_path / "slides.pdf").write_text("pdf")
    monkeypatch.setattr("compile_loop.shutil.which", lambda b: "/usr/bin/tectonic")
    monkeypatch.setattr("compile_loop.subprocess.run", lambda *a, **k: _Proc(0))
    res = CompileLoop().compile(str(tex))
    assert res.success and res.attempts == 1


def test_retry_then_give_up(monkeypatch, tmp_path):
    tex = tmp_path / "slides.tex"; tex.write_text("x")
    monkeypatch.setattr("compile_loop.shutil.which", lambda b: "/usr/bin/tectonic")
    monkeypatch.setattr("compile_loop.subprocess.run", lambda *a, **k: _Proc(1))
    calls = {"n": 0}
    def _repair(**k):
        calls["n"] += 1
        return {"choices": [{"message": {"content": "fixed"}}]}
    monkeypatch.setattr("compile_loop.litellm.completion", _repair)
    res = CompileLoop().compile(str(tex), max_retries=5)
    assert res.success is False and res.attempts == 5
    assert calls["n"] == 4  # repair called between attempts, not after the last


def test_fallback_to_pdflatex(monkeypatch, tmp_path):
    tex = tmp_path / "slides.tex"; tex.write_text("x")
    (tmp_path / "slides.pdf").write_text("pdf")
    monkeypatch.setattr("compile_loop.shutil.which",
                        lambda b: None if b == "tectonic" else "/usr/bin/pdflatex")
    captured = {}
    def _run(cmd, **k):
        captured["cmd"] = cmd
        return _Proc(0)
    monkeypatch.setattr("compile_loop.subprocess.run", _run)
    res = CompileLoop().compile(str(tex))
    assert res.success and captured["cmd"][0] == "pdflatex"


def test_no_engine_returns_error(monkeypatch, tmp_path):
    tex = tmp_path / "slides.tex"; tex.write_text("x")
    monkeypatch.setattr("compile_loop.shutil.which", lambda b: None)
    res = CompileLoop().compile(str(tex), max_retries=2)
    assert res.success is False and "PATH" in res.final_error
```

### Task 8.5 — `test_figure_triage.py`

- [ ] Mock `litellm.completion` and PNG read. Assert score parsing and that one bad figure does not abort the batch.

```python
import pytest

import figure_triage
from figure_triage import FigureTriage

pytestmark = pytest.mark.mocked


def test_score_figure(monkeypatch):
    monkeypatch.setattr(figure_triage, "_encode_png", lambda p: "AAAA")
    monkeypatch.setattr(figure_triage.litellm, "completion", lambda **k: {
        "choices": [{"message": {"content":
            '{"slide_worthy": true, "score": 0.9, "description": "clear"}'}}]})
    fs = FigureTriage().score_figure("/tmp/fig1.png", "cap")
    assert fs.slide_worthy and fs.score == 0.9


def test_triage_survives_bad_figure(monkeypatch):
    def _boom(p): raise FileNotFoundError(p)
    monkeypatch.setattr(figure_triage, "_encode_png", _boom)
    out = FigureTriage().triage([{"path": "/tmp/missing.png", "caption": ""}])
    assert len(out) == 1 and out[0].slide_worthy is False
```

### Task 8.6 — Server-level ordering test (in `test_compile_loop.py` or a new `test_server.py`)

- [ ] Verify `generate` runs stages in order and emits the output contract. Patch each stage to a sentinel; assert call order and JSON keys.

```python
import json
import pytest

pytestmark = pytest.mark.mocked


def test_generate_pipeline_order(monkeypatch, tmp_path, parsed_paper):
    import server
    pp = tmp_path / "paper.json"; pp.write_text(json.dumps(parsed_paper))
    order = []

    class _BP:
        slides = [1, 2, 3]
    monkeypatch.setattr(server.OutlinePlanner, "plan",
                        lambda self, p, d: order.append("outline") or _BP())
    monkeypatch.setattr(server.SlideGenerator, "generate",
                        lambda self, bp, o, f: order.append("gen") or str(tmp_path / "slides.tex"))

    class _Res:
        success = True; pdf_path = str(tmp_path / "slides.pdf")
    monkeypatch.setattr(server.CompileLoop, "compile",
                        lambda self, p: order.append("compile") or _Res())

    out = json.loads(server._run_generate(
        {"parsed_paper_path": str(pp), "talk_duration_min": 20}))
    assert order == ["outline", "gen", "compile"]
    assert out["slide_count"] == 3 and out["compile_success"] is True
    assert set(out) == {"tex_path", "pdf_path", "notes_path",
                        "slide_count", "compile_success"}
```

### Task 8.7 — No-stdout regression test

- [ ] Assert that importing the server and running a stage writes nothing to stdout.

```python
import io
import json
import sys
import pytest

pytestmark = pytest.mark.mocked


def test_no_stdout_writes(monkeypatch, tmp_path, parsed_paper, capsys):
    import server
    pp = tmp_path / "paper.json"; pp.write_text(json.dumps(parsed_paper))
    monkeypatch.setattr(server.OutlinePlanner, "plan",
                        lambda self, p, d: type("B", (), {"slides": []})())
    monkeypatch.setattr(server.SlideGenerator, "generate",
                        lambda self, bp, o, f: str(tmp_path / "slides.md"))
    server._run_generate({"parsed_paper_path": str(pp), "output_format": "marp"})
    captured = capsys.readouterr()
    assert captured.out == ""   # stdout must be empty; logs go to stderr
```

---

## Phase 9 — Verification

### Task 9.1 — Run the mocked test suite

- [ ] All tests pass with no GPU and no LaTeX install.

```bash
cd /Users/zachstallbohm/Work/gemma && \
python -m pytest tests/services/skills/paper-to-slides -m mocked -q
```

### Task 9.2 — Grep for forbidden patterns

- [ ] Confirm: no `print(`, no `tiktoken`, no `QWEN`, no `console.log`.

```bash
cd /Users/zachstallbohm/Work/gemma/services/skills/paper-to-slides && \
! grep -rn "print(" . --include="*.py" && \
! grep -rni "tiktoken\|qwen" . --include="*.py" && \
echo "clean"
```

### Task 9.3 — Smoke-check tectonic detection

- [ ] Confirm fallback messaging is correct on a host without tectonic (optional, manual).

```bash
which tectonic || echo "tectonic absent — CompileLoop will fall back to pdflatex"
```

---

## Done criteria

- [ ] All three tools (`generate`, `generate_outline`, `compile_tex`) implemented and listed by the server.
- [ ] `SlideBlueprint`, `PresentationBlueprint`, `CompileResult` named and used consistently across modules.
- [ ] Every LLM call (outline, repair, vision triage, notes) targets `GEMMA_BASE`; zero Qwen references.
- [ ] tectonic primary with documented pdflatex fallback and a clear error when neither is present.
- [ ] All logging to stderr; the no-stdout test passes.
- [ ] Mocked test suite green without GPU or LaTeX.
