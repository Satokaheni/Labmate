---
name: ast-repo-map
description: >
  Builds a ranked repository map for code navigation. Use when the agent needs
  to understand the structure of a codebase, locate symbols, or select which
  files to edit. Emits a token-budgeted JSONL of the most important function
  and class definitions ranked by PageRank over the call graph.
trigger: "Use when starting a new task requiring codebase orientation"
tools:
  - ast.repo-map.get_repo_map
  - ast.repo-map.get_symbols
version: "0.2.0"
license: MIT
requires: []
---

# AST Repo Map Skill

You have access to the `ast.repo-map` MCP server which provides structured,
language-aware codebase navigation without reading raw source files.

## When to Use

Use this skill at the beginning of any task that requires understanding existing
code structure:

- Locating a function or class by name across a large codebase
- Identifying which files will be affected by a change
- Building context before editing so you do not over-read files

## Available Tools

### `ast.repo-map.get_repo_map`

Returns a JSONL list of the most important symbols in the repository, ranked
by personalized PageRank and bounded by a configurable token budget.

```json
{
  "chat_files": ["src/service.py"],
  "max_tokens": 2000
}
```

Each output line: `{"name": "...", "kind": "function|class|method", "signature": "...", "parent": "...", "loc": "path/to/file:42"}`

### `ast.repo-map.get_symbols`

Returns all symbols defined in a specific file.

```json
{ "file": "src/service.py" }
```

## Workflow

1. Call `get_repo_map` with the files you are actively editing in `chat_files`.
2. Review the JSONL output to understand the symbol landscape.
3. Use `loc` fields to target reads precisely — read only the functions you
   need, not entire files.
4. Re-call `get_repo_map` after edits so the map reflects current state.

## Limitations

- Does not resolve types across files — use the `ts-refactor` skill for
  type-aware TypeScript operations.
- Symbol map may lag by ~1 second after file edits (cache is mtime-keyed).
- Token budget is hard-capped; large monorepos will see truncation.
