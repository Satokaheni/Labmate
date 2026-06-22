---
name: component-doc-gen
description: >
  Auto-generates TypeScript prop tables, markdown documentation, and Storybook
  CSF3 stories from React component source files using AST analysis (ts-morph).
  Deterministic — no LLM required. Use when documenting generated or existing
  React components. Extracts props, types, required/optional status, and JSDoc.
trigger: "Use when generating documentation or Storybook stories for React components"
tools:
  - generate
  - generate_batch
version: "0.1.0"
license: MIT
requires: []
---

# Component Doc Gen Skill

You have access to the `component-doc-gen` MCP server, which generates React
component documentation directly from the TypeScript source via `ts-morph`. It
finds the component's Props interface (or `Props` type alias), extracts each
prop's name, type, required/optional status, default value, and JSDoc
description, then renders a markdown prop table, a full markdown doc, and a
Storybook CSF3 story. Everything is AST-derived and deterministic — no LLM call
is made by default.

## When to Use

- Document a newly generated or existing React component (prop table + markdown).
- Produce a starter Storybook CSF3 story for a component.
- Batch-document an entire components directory.

## Critical Rules

- `component_path` and `dir_path` MUST be absolute paths. ts-morph misinterprets
  relative paths.
- Output is generated, not written to disk — the tools return the documentation
  as JSON/JSONL for the caller to place where it wants.
- Optional LLM enrichment: if the `GEMMA_BASE` env var is set, a one-paragraph
  human-readable description is added to the markdown doc. With it unset (the
  default), the description is empty and the run is fully offline/deterministic.

## Available Tools

### `generate`

Generate docs (and, by default, a Storybook story) for a single component file.

```json
{
  "component_path": "/abs/path/to/src/Button.tsx",
  "include_stories": true
}
```

Returns a JSON `ComponentDoc`:
`{ "component_name": "...", "file_path": "...", "props": [...], "props_table": "...", "story_code": "...", "markdown_doc": "..." }`.

### `generate_batch`

Generate docs for every component matching a glob under a directory.

```json
{
  "dir_path": "/abs/path/to/src/components",
  "pattern": "**/*.tsx"
}
```

Returns JSONL — one `ComponentDoc` JSON object per line. A file that fails to
parse yields a `{ "file_path": "...", "error": "..." }` line instead of aborting
the batch.

## Limitations

- React/TypeScript only. Props must be declared as an interface or a type-literal
  alias named `<Component>Props` or `Props` (or ending in `Props`).
- Props spread from imported/extended types in other files are not resolved
  (single-file AST, no project-wide type checker).
- The Storybook story is a starter template (one `Default` story with required
  args filled by best-effort sample values), not a full interaction test.
