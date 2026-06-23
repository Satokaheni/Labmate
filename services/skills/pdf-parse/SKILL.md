---
name: pdf-parse
description: >
  Converts scientific PDFs to structured Markdown with extracted figures, tables, and formulas.
  Use when you need to read a PDF paper, extract its text content for downstream processing,
  or extract figures and tables. Prerequisite for paper-rag and academic-writing skills.
  Default mode uses Docling (CPU). Use mode=mineru for higher fidelity (requires GPU).
trigger: "Use when reading or extracting content from a PDF file"
tools:
  - parse
  - parse_batch
  - extract_figures
version: "0.1.0"
license: MIT
requires: []
---

# PDF Parse Skill

You have access to the `pdf_parse` MCP server, which converts scientific PDFs
into clean Markdown plus structured figures, tables, and metadata. Use it
instead of trying to read raw PDF bytes.

## When to Use

- Reading a PDF paper so its text can be summarized, critiqued, or cited
- Extracting a paper's content for ingestion by `paper-rag`
- Pulling figures (with captions) or tables (as HTML) out of a PDF

## Available Tools

### `parse`

Parse one PDF. Returns a JSON object: `markdown`, `figures`, `tables`, `metadata`.

```json
{ "path": "/papers/attention.pdf", "mode": "docling" }
```

### `parse_batch`

Parse several PDFs. Returns JSONL, one result object per line, in input order.

```json
{ "paths": ["/papers/a.pdf", "/papers/b.pdf"], "mode": "docling" }
```

### `extract_figures`

Extract only figures (image path + caption + page) as a JSON list.

```json
{ "path": "/papers/attention.pdf" }
```

## Modes

- `docling` (default): CPU-friendly, good general fidelity. Always available.
- `mineru`: higher fidelity for dense layouts and formulas; requires the
  `mineru` package and a GPU. If unavailable, the tool returns an error
  telling you to install it.

## Output Contract

`parse` returns:

```json
{
  "path": "...",
  "markdown": "# Title\n\n...",
  "figures": [{"path": "/tmp/pdf-parse-assets/fig1.png", "caption": "...", "page": 3}],
  "tables":  [{"html": "<table>...</table>", "caption": "...", "page": 5}],
  "metadata": {"title": "...", "authors": ["..."], "doi": "...", "page_count": 12}
}
```

## Limitations

- Scanned PDFs without an OCR layer may yield sparse text in docling mode.
- Figure/caption pairing is heuristic; verify captions for critical use.
- `mineru` mode needs GPU; on CPU-only hosts use the default `docling` mode.
