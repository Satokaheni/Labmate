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

```json
{ "tex_path": "...", "pdf_path": "...", "notes_path": null,
  "slide_count": 13, "compile_success": true }
```
