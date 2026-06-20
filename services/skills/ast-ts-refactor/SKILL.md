---
name: ast-ts-refactor
description: >
  Type-aware cross-file TypeScript and JavaScript refactoring using ts-morph.
  Use when you need to rename a symbol across the whole project (including through
  re-exports), find all references to a TypeScript symbol, or move a symbol
  between files. This is the only tool that resolves references correctly through
  the TypeScript type checker.
trigger: "Use for TypeScript/JS cross-file rename, find-references, or move-symbol"
tools:
  - rename_symbol
  - find_references
  - move_symbol
version: "0.1.0"
license: MIT
requires: []
---

# AST TypeScript Refactor Skill

You have access to the `ast.ts-refactor` MCP server, which performs type-aware,
cross-file refactoring of TypeScript and JavaScript via the `ts-morph` wrapper
around the TypeScript compiler API. Unlike text search or ast-grep, it resolves
references through the type checker — so it correctly follows re-exports, barrel
imports, and type aliases, and never renames an unrelated symbol that happens to
share a name.

## When to Use

- Rename a symbol (function, class, interface, type, enum, variable) across the
  entire project, including every import and re-export.
- Find all usage sites of a symbol, including barrel-file re-exports.
- Move a symbol to a different file and have all imports rewritten automatically.

Do NOT use ast-grep for these operations — it is syntactic and cannot resolve
cross-file references or shadowed names.

## Critical Rules

- `tsconfig` MUST be an absolute path. Relative paths are misinterpreted by
  ts-morph and will silently load the wrong (or empty) project.
- All edits are held in memory and are NOT written to disk. Each tool returns a
  unified diff describing the pending changes. Review the diff before confirming
  any save. The server never auto-saves.

## Available Tools

### `rename_symbol`

Renames `symbol` (declared in `file`) to `new_name` across the whole project.

```json
{
  "tsconfig": "/abs/path/to/tsconfig.json",
  "file": "src/order.ts",
  "symbol": "computeTotal",
  "new_name": "computeOrderTotal"
}
```

Returns a `Diff`: `{ "unified_diff": "...", "files_affected": ["..."], "changes": N }`.

### `find_references`

Returns every usage site of `symbol`, including re-exports and barrel imports.

```json
{
  "tsconfig": "/abs/path/to/tsconfig.json",
  "file": "src/order.ts",
  "symbol": "computeTotal"
}
```

Returns a list of `Reference`:
`{ "file": "...", "line": N, "column": N, "text": "...", "is_definition": false }`.

### `move_symbol`

Moves `symbol` from `source_file` to `dest_file`, rewriting imports in all
affected files.

```json
{
  "tsconfig": "/abs/path/to/tsconfig.json",
  "source_file": "src/order.ts",
  "symbol": "computeTotal",
  "dest_file": "src/totals.ts"
}
```

Returns a `Diff`.

## Workflow

1. Call `find_references` first to understand the blast radius of a rename or move.
2. Call `rename_symbol` or `move_symbol`; read the returned unified diff.
3. Confirm the change explicitly before any save step. If the diff is wrong,
   discard — nothing has been written to disk.

## Limitations

- TypeScript/JavaScript only. For Python use a rope/jedi skill; for Rust use
  rust-analyzer.
- A symbol that is not exported and not a top-level declaration in `file` may not
  be locatable; pass the file where the declaration actually lives.
- The returned unified diff is a human/model-readable preview, not a `git apply`
  patch. The authoritative pending state is held in the server's in-memory project.
