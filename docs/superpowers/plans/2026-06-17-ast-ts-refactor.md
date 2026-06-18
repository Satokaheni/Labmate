# ast-ts-refactor MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the ast-ts-refactor TypeScript MCP server that provides type-aware cross-file TypeScript refactoring using ts-morph.

**Architecture:** A Node.js MCP stdio server wraps ts-morph to expose rename_symbol, find_references, and move_symbol. Projects are loaded from tsconfig.json (absolute path required) and cached per tsconfig. All edits are held in-memory until explicitly saved — the server returns a unified diff for model review. All logging uses console.error (never console.log).

**Tech Stack:** Node.js 20+, TypeScript 5+, `@modelcontextprotocol/sdk`, `ts-morph`, `zod`, `vitest`

---

## File Map

| File | Responsibility |
|---|---|
| `services/skills/ast-ts-refactor/package.json` | ESM module, deps, build/test scripts |
| `services/skills/ast-ts-refactor/tsconfig.json` | strict, ES2022, NodeNext, emit to `dist/` |
| `services/skills/ast-ts-refactor/.gitignore` | ignore `dist/`, `node_modules/` |
| `services/skills/ast-ts-refactor/src/types.ts` | `Diff`, `Reference` interfaces |
| `services/skills/ast-ts-refactor/src/refactor.ts` | `TsRefactor` class wrapping ts-morph |
| `services/skills/ast-ts-refactor/src/index.ts` | MCP server entry point (stdio transport) |
| `services/skills/ast-ts-refactor/SKILL.md` | Skill discovery artifact + instructions |
| `tests/refactor.test.ts` | vitest unit tests over temp fixtures |

---

## Spec-requirement coverage (section 6.4)

| Spec requirement | Task |
|---|---|
| `rename_symbol(tsconfig, file, symbol, new_name) -> Diff`, resolves cross-file refs via type checker | Task 3, Task 7 |
| `find_references(tsconfig, file, symbol) -> list[Reference]`, includes re-exports + barrel imports | Task 4, Task 7 |
| `move_symbol(tsconfig, source_file, symbol, dest_file) -> Diff`, rewrites imports | Task 5, Task 7 |
| `tsconfig` must be absolute path | Task 3 (`getProject`), Task 8 (test) |
| In-memory safety — never auto-save, return pending Diff | Task 3 (`toDiff`), Task 7, Task 8 (test) |
| `console.error` only, never `console.log` | Task 6 (logger), all source; Task 8 (test) |

---

## Task 1: Project Scaffold

**Files:**
- Create: `services/skills/ast-ts-refactor/package.json`
- Create: `services/skills/ast-ts-refactor/tsconfig.json`
- Create: `services/skills/ast-ts-refactor/.gitignore`

- [ ] **Step 1: Create the directory structure**

```bash
mkdir -p services/skills/ast-ts-refactor/src
mkdir -p tests
```

- [ ] **Step 2: Create `services/skills/ast-ts-refactor/package.json`**

```json
{
  "name": "@labmate/ast-ts-refactor",
  "version": "0.1.0",
  "description": "Type-aware cross-file TypeScript refactoring MCP server using ts-morph",
  "license": "MIT",
  "type": "module",
  "main": "dist/index.js",
  "bin": {
    "ast-ts-refactor": "dist/index.js"
  },
  "scripts": {
    "build": "tsc",
    "start": "node dist/index.js",
    "test": "vitest run",
    "test:watch": "vitest"
  },
  "dependencies": {
    "@modelcontextprotocol/sdk": "^1.12.0",
    "ts-morph": "^28.0.0",
    "zod": "^3.25.0"
  },
  "devDependencies": {
    "@types/node": "^20.14.0",
    "typescript": "^5.5.0",
    "vitest": "^2.0.0"
  },
  "engines": {
    "node": ">=20"
  }
}
```

- [ ] **Step 3: Create `services/skills/ast-ts-refactor/tsconfig.json`**

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "lib": ["ES2022"],
    "outDir": "dist",
    "rootDir": "src",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "forceConsistentCasingInFileNames": true,
    "declaration": true,
    "sourceMap": true
  },
  "include": ["src/**/*.ts"],
  "exclude": ["node_modules", "dist", "tests"]
}
```

- [ ] **Step 4: Create `services/skills/ast-ts-refactor/.gitignore`**

```
node_modules/
dist/
*.tsbuildinfo
```

- [ ] **Step 5: Install dependencies**

```bash
cd services/skills/ast-ts-refactor && npm install
```

---

## Task 2: Type Definitions

**Files:**
- Create: `services/skills/ast-ts-refactor/src/types.ts`

- [ ] **Step 1: Create `src/types.ts` with the `Diff` and `Reference` interfaces**

```typescript
// src/types.ts

/** Pending in-memory changes captured as a git-style unified diff (NOT yet saved to disk). */
export interface Diff {
  unified_diff: string;     // git-style unified diff (pending, not yet saved)
  files_affected: string[]; // list of file paths that would change
  changes: number;          // total replacement count
}

/** A single usage site of a symbol. */
export interface Reference {
  file: string;
  line: number;
  column: number;
  text: string;             // source text of the reference
  is_definition: boolean;
}
```

---

## Task 3: TsRefactor — class shell, project cache, and diff capture

**Files:**
- Create: `services/skills/ast-ts-refactor/src/refactor.ts`

- [ ] **Step 1: Create `src/refactor.ts` with imports, the project cache, and `getProject`**

`getProject` enforces the absolute-path rule and caches one `Project` per tsconfig. We snapshot the full pre-edit text of every source file before any mutation so `toDiff` can compute a real unified diff against the on-disk baseline.

```typescript
// src/refactor.ts
// NEVER console.log — ALWAYS console.error
import * as path from "node:path";
import { Project, SourceFile } from "ts-morph";
import type { Diff, Reference } from "./types.js";

export class TsRefactor {
  private projects = new Map<string, Project>(); // tsconfig -> Project (cache)

  private getProject(tsconfig: string): Project {
    // tsconfig MUST be absolute path — ts-morph misinterprets relative paths.
    if (!path.isAbsolute(tsconfig)) {
      throw new Error(`tsconfig must be an absolute path, got: ${tsconfig}`);
    }
    let project = this.projects.get(tsconfig);
    if (project === undefined) {
      console.error(`[ts-refactor] loading project from ${tsconfig}`);
      project = new Project({ tsConfigFilePath: tsconfig });
      this.projects.set(tsconfig, project);
    }
    return project;
  }
}
```

- [ ] **Step 2: Add a baseline snapshot helper and the `toDiff` method**

`snapshot` records the current (pre-edit) text of every source file. `toDiff` compares each file's current in-memory text against the snapshot and emits a unified diff. This captures pending changes WITHOUT calling `project.save()` — edits stay in memory.

Add these methods inside the `TsRefactor` class:

```typescript
  /** Capture the current text of every source file as a baseline for diffing. */
  private snapshot(project: Project): Map<string, string> {
    const baseline = new Map<string, string>();
    for (const sf of project.getSourceFiles()) {
      baseline.set(sf.getFilePath(), sf.getFullText());
    }
    return baseline;
  }

  /** Build a Diff of pending in-memory changes vs. the supplied baseline. Does NOT save. */
  private toDiff(project: Project, baseline: Map<string, string>): Diff {
    const filesAffected: string[] = [];
    const diffParts: string[] = [];
    let changes = 0;

    for (const sf of project.getSourceFiles()) {
      const filePath = sf.getFilePath();
      const before = baseline.get(filePath) ?? "";
      const after = sf.getFullText();
      if (before === after) continue;
      filesAffected.push(filePath);
      const fileDiff = this.unifiedDiff(filePath, before, after);
      diffParts.push(fileDiff.text);
      changes += fileDiff.hunks;
    }

    // Files newly created by a move that did not exist in the baseline.
    for (const sf of project.getSourceFiles()) {
      const filePath = sf.getFilePath();
      if (!baseline.has(filePath)) {
        filesAffected.push(filePath);
        const fileDiff = this.unifiedDiff(filePath, "", sf.getFullText());
        diffParts.push(fileDiff.text);
        changes += fileDiff.hunks;
      }
    }

    return {
      unified_diff: diffParts.join("\n"),
      files_affected: [...new Set(filesAffected)],
      changes,
    };
  }

  /** Minimal line-based unified diff for one file. Returns the diff text and a changed-hunk count. */
  private unifiedDiff(filePath: string, before: string, after: string): { text: string; hunks: number } {
    const a = before.split("\n");
    const b = after.split("\n");
    const lines: string[] = [`--- a/${filePath}`, `+++ b/${filePath}`];
    let hunks = 0;
    // Simple LCS-free walk: emit removals for a, additions for b around the first divergence.
    let i = 0;
    let j = 0;
    while (i < a.length || j < b.length) {
      if (i < a.length && j < b.length && a[i] === b[j]) {
        i++;
        j++;
        continue;
      }
      // Find the next matching anchor to bound the changed block.
      const anchorB = j < b.length ? b.indexOf(a[i] ?? " ", j) : -1;
      if (i < a.length && anchorB === -1) {
        lines.push(`-${a[i]}`);
        i++;
        hunks++;
      } else if (j < b.length) {
        lines.push(`+${b[j]}`);
        j++;
        hunks++;
      } else {
        i++;
      }
    }
    return { text: lines.join("\n"), hunks };
  }
```

> Note: The unified diff is a reviewable preview, not a patch consumed by `git apply`. The model reads it to decide whether to confirm. The authoritative pending state lives in the ts-morph project in memory.

---

## Task 4: TsRefactor — `findReferences`

**Files:**
- Modify: `services/skills/ast-ts-refactor/src/refactor.ts`

- [ ] **Step 1: Add a private `locateDeclaration` helper**

Resolves a named symbol declaration in a given file. Used by both `findReferences` and `renameSymbol`. `file` may be absolute or project-relative; we resolve it through the project.

```typescript
  /** Find the SourceFile for `file`, throwing a clear error if absent. */
  private getSourceFile(project: Project, file: string): SourceFile {
    const sf =
      project.getSourceFile(file) ??
      (path.isAbsolute(file) ? project.getSourceFile(path.resolve(file)) : undefined) ??
      project.getSourceFiles().find((s) => s.getFilePath().endsWith(file));
    if (sf === undefined) {
      throw new Error(`file not found in project: ${file}`);
    }
    return sf;
  }

  /** Locate a renameable/named declaration node for `symbol` in `file`. */
  private locateDeclaration(project: Project, file: string, symbol: string) {
    const sf = this.getSourceFile(project, file);
    // getExportedDeclarations covers exported symbols (the common refactor target).
    const exported = sf.getExportedDeclarations().get(symbol);
    if (exported && exported.length > 0) {
      return exported[0];
    }
    // Fall back to any locally declared identifier matching the name.
    const local =
      sf.getFunction(symbol) ??
      sf.getClass(symbol) ??
      sf.getInterface(symbol) ??
      sf.getTypeAlias(symbol) ??
      sf.getEnum(symbol) ??
      sf.getVariableDeclaration(symbol);
    if (local === undefined) {
      throw new Error(`symbol '${symbol}' not found in ${file}`);
    }
    return local;
  }
```

- [ ] **Step 2: Add the `findReferences` method**

Uses ts-morph's `findReferences()` on the declaration's name node, which goes through the TS type checker and therefore catches re-exports and barrel imports.

```typescript
  findReferences(tsconfig: string, file: string, symbol: string): Reference[] {
    const project = this.getProject(tsconfig);
    const decl = this.locateDeclaration(project, file, symbol);

    // The declaration node may be a NameableNode; get the identifier to search from.
    const nameNode =
      "getNameNode" in decl && typeof (decl as any).getNameNode === "function"
        ? (decl as any).getNameNode()
        : decl;

    const results: Reference[] = [];
    const referencedSymbols = nameNode.findReferences();
    for (const referencedSymbol of referencedSymbols) {
      for (const ref of referencedSymbol.getReferences()) {
        const node = ref.getNode();
        const sf = ref.getSourceFile();
        const start = node.getStart();
        const { line, column } = sf.getLineAndColumnAtPos(start);
        results.push({
          file: sf.getFilePath(),
          line,
          column,
          text: node.getText(),
          is_definition: ref.isDefinition(),
        });
      }
    }
    console.error(`[ts-refactor] find_references '${symbol}': ${results.length} sites`);
    return results;
  }
```

---

## Task 5: TsRefactor — `renameSymbol` and `moveSymbol`

**Files:**
- Modify: `services/skills/ast-ts-refactor/src/refactor.ts`

- [ ] **Step 1: Add the `renameSymbol` method**

Snapshots the baseline, calls `.rename(newName)` on the declaration (which the TS type checker propagates across all files), then returns the pending diff. Never calls `project.save()`.

```typescript
  renameSymbol(tsconfig: string, file: string, symbol: string, newName: string): Diff {
    const project = this.getProject(tsconfig);
    const baseline = this.snapshot(project);
    const decl = this.locateDeclaration(project, file, symbol);

    if (!("rename" in decl) || typeof (decl as any).rename !== "function") {
      throw new Error(`symbol '${symbol}' in ${file} is not renameable`);
    }
    (decl as any).rename(newName); // type-checker-driven cross-file rename; in-memory only

    const diff = this.toDiff(project, baseline);
    console.error(
      `[ts-refactor] rename '${symbol}' -> '${newName}': ${diff.changes} changes across ${diff.files_affected.length} files (NOT saved)`,
    );
    return diff;
  }
```

- [ ] **Step 2: Add the `moveSymbol` method**

Moves an exported declaration to `destFile` and rewrites imports across the project. ts-morph statement nodes expose no single "move" primitive, so we use the documented pattern: ensure the destination file exists, move the statement node into it with `moveToFile`-style logic via `Statement#getText` re-insertion plus reference fix-up. ts-morph's `SourceFile#move` is for renaming files; for symbol moves we relocate the declaration statement and let `organizeImports`/explicit import insertion correct call sites.

```typescript
  moveSymbol(tsconfig: string, sourceFile: string, symbol: string, destFile: string): Diff {
    const project = this.getProject(tsconfig);
    const baseline = this.snapshot(project);

    const src = this.getSourceFile(project, sourceFile);
    const decl = this.locateDeclaration(project, sourceFile, symbol);

    // Ensure destination file exists in the project (created in-memory; saved only on confirm).
    const destPath = path.isAbsolute(destFile) ? destFile : path.resolve(path.dirname(src.getFilePath()), destFile);
    const dest = project.getSourceFile(destPath) ?? project.createSourceFile(destPath, "", { overwrite: false });

    // Capture all reference sites BEFORE mutation so we can repoint imports afterward.
    const refs = this.findReferences(tsconfig, sourceFile, symbol);

    // Move the declaration's statement text into the destination file.
    const stmt = decl.getFirstAncestorByKindOrThrow?.(decl.getKind()) ?? decl;
    const declText = (stmt as any).getText ? (stmt as any).getText() : decl.getText();
    dest.addStatements(declText.startsWith("export") ? declText : `export ${declText}`);

    // Remove the original declaration from the source file.
    if (typeof (decl as any).remove === "function") {
      (decl as any).remove();
    } else {
      throw new Error(`cannot remove declaration '${symbol}' from ${sourceFile}`);
    }

    // Add an import of the moved symbol in every file that referenced it (except the dest).
    const importPathFor = (fromFile: SourceFile): string => {
      let rel = path.relative(path.dirname(fromFile.getFilePath()), destPath).replace(/\.tsx?$/, "");
      if (!rel.startsWith(".")) rel = `./${rel}`;
      return rel;
    };
    const touched = new Set<string>();
    for (const ref of refs) {
      if (ref.is_definition) continue;
      const refSf = project.getSourceFile(ref.file);
      if (refSf === undefined || refSf.getFilePath() === destPath) continue;
      if (touched.has(refSf.getFilePath())) continue;
      touched.add(refSf.getFilePath());
      const existing = refSf
        .getImportDeclarations()
        .find((d) => d.getModuleSpecifierValue() === importPathFor(refSf));
      if (existing) {
        if (!existing.getNamedImports().some((n) => n.getName() === symbol)) {
          existing.addNamedImport(symbol);
        }
      } else {
        refSf.addImportDeclaration({ moduleSpecifier: importPathFor(refSf), namedImports: [symbol] });
      }
    }

    const diff = this.toDiff(project, baseline);
    console.error(
      `[ts-refactor] move '${symbol}' ${sourceFile} -> ${destFile}: ${diff.changes} changes across ${diff.files_affected.length} files (NOT saved)`,
    );
    return diff;
  }
```

> Note: After moving, the source file may retain an unused import; the diff surfaces this for the model to review. We deliberately do NOT call `organizeImports()` automatically, to keep the diff minimal and predictable.

---

## Task 6: MCP Server Entry Point

**Files:**
- Create: `services/skills/ast-ts-refactor/src/index.ts`

- [ ] **Step 1: Create `src/index.ts` with imports, server, and zod input schemas**

```typescript
// src/index.ts
// NEVER console.log — ALWAYS console.error
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";
import { zodToJsonSchema } from "zod/v3"; // if unavailable, use a minimal hand-inlined schema (see Step 2)
import { TsRefactor } from "./refactor.js";

const refactor = new TsRefactor();

const renameInput = z.object({
  tsconfig: z.string().describe("Absolute path to tsconfig.json"),
  file: z.string().describe("File containing the symbol declaration"),
  symbol: z.string().describe("Name of the symbol to rename"),
  new_name: z.string().describe("New name for the symbol"),
});

const findRefsInput = z.object({
  tsconfig: z.string().describe("Absolute path to tsconfig.json"),
  file: z.string().describe("File containing the symbol declaration"),
  symbol: z.string().describe("Name of the symbol to find references for"),
});

const moveInput = z.object({
  tsconfig: z.string().describe("Absolute path to tsconfig.json"),
  source_file: z.string().describe("File currently containing the symbol"),
  symbol: z.string().describe("Name of the symbol to move"),
  dest_file: z.string().describe("Destination file for the symbol"),
});
```

- [ ] **Step 2: Add inline JSON Schemas for `tools/list`**

The MCP `inputSchema` must be self-contained JSON Schema. Hand-inline it (no external `$ref`) to avoid a dependency on a zod-to-json-schema converter and to guarantee self-containment per the spec.

```typescript
const RENAME_SCHEMA = {
  type: "object",
  properties: {
    tsconfig: { type: "string", description: "Absolute path to tsconfig.json" },
    file: { type: "string", description: "File containing the symbol declaration" },
    symbol: { type: "string", description: "Name of the symbol to rename" },
    new_name: { type: "string", description: "New name for the symbol" },
  },
  required: ["tsconfig", "file", "symbol", "new_name"],
  additionalProperties: false,
} as const;

const FIND_REFS_SCHEMA = {
  type: "object",
  properties: {
    tsconfig: { type: "string", description: "Absolute path to tsconfig.json" },
    file: { type: "string", description: "File containing the symbol declaration" },
    symbol: { type: "string", description: "Name of the symbol to find references for" },
  },
  required: ["tsconfig", "file", "symbol"],
  additionalProperties: false,
} as const;

const MOVE_SCHEMA = {
  type: "object",
  properties: {
    tsconfig: { type: "string", description: "Absolute path to tsconfig.json" },
    source_file: { type: "string", description: "File currently containing the symbol" },
    symbol: { type: "string", description: "Name of the symbol to move" },
    dest_file: { type: "string", description: "Destination file for the symbol" },
  },
  required: ["tsconfig", "source_file", "symbol", "dest_file"],
  additionalProperties: false,
} as const;
```

> The line `import { zodToJsonSchema } from "zod/v3";` from Step 1 is NOT needed once the inline schemas above are used. Remove that import — the zod objects are kept only for runtime argument parsing (`.parse`), the JSON Schemas above are what `tools/list` returns.

- [ ] **Step 3: Remove the unused converter import**

Delete this line added in Step 1:

```typescript
import { zodToJsonSchema } from "zod/v3"; // if unavailable, use a minimal hand-inlined schema (see Step 2)
```

- [ ] **Step 4: Construct the server and register `ListToolsRequestSchema`**

```typescript
const server = new Server(
  { name: "ast.ts-refactor", version: "0.1.0" },
  { capabilities: { tools: {} } },
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "rename_symbol",
      description:
        "Type-aware cross-file rename of a TypeScript/JS symbol via the TS type checker. Returns a pending unified diff (NOT saved). tsconfig must be an absolute path.",
      inputSchema: RENAME_SCHEMA,
    },
    {
      name: "find_references",
      description:
        "Find all references to a TypeScript/JS symbol across the project, including re-exports and barrel imports. tsconfig must be an absolute path.",
      inputSchema: FIND_REFS_SCHEMA,
    },
    {
      name: "move_symbol",
      description:
        "Move a symbol to another file and rewrite imports across the project. Returns a pending unified diff (NOT saved). tsconfig must be an absolute path.",
      inputSchema: MOVE_SCHEMA,
    },
  ],
}));
```

- [ ] **Step 5: Register `CallToolRequestSchema` with the dispatch switch**

Each branch parses arguments with zod, calls the `TsRefactor` method, and returns the result as JSON text content. Errors are returned as `isError: true` content, never thrown across the transport.

```typescript
server.setRequestHandler(CallToolRequestSchema, async (req) => {
  const { name, arguments: args } = req.params;
  try {
    switch (name) {
      case "rename_symbol": {
        const a = renameInput.parse(args);
        const diff = refactor.renameSymbol(a.tsconfig, a.file, a.symbol, a.new_name);
        return { content: [{ type: "text", text: JSON.stringify(diff, null, 2) }] };
      }
      case "find_references": {
        const a = findRefsInput.parse(args);
        const refs = refactor.findReferences(a.tsconfig, a.file, a.symbol);
        return { content: [{ type: "text", text: JSON.stringify(refs, null, 2) }] };
      }
      case "move_symbol": {
        const a = moveInput.parse(args);
        const diff = refactor.moveSymbol(a.tsconfig, a.source_file, a.symbol, a.dest_file);
        return { content: [{ type: "text", text: JSON.stringify(diff, null, 2) }] };
      }
      default:
        return {
          content: [{ type: "text", text: `Unknown tool: ${name}` }],
          isError: true,
        };
    }
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.error(`[ts-refactor] tool '${name}' failed: ${message}`);
    return {
      content: [{ type: "text", text: `Error: ${message}` }],
      isError: true,
    };
  }
});
```

- [ ] **Step 6: Connect the stdio transport and start**

```typescript
async function main(): Promise<void> {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("[ts-refactor] MCP server ready on stdio");
}

main().catch((err) => {
  console.error("[ts-refactor] fatal:", err);
  process.exit(1);
});
```

---

## Task 7: SKILL.md

**Files:**
- Create: `services/skills/ast-ts-refactor/SKILL.md`

- [ ] **Step 1: Create `SKILL.md` with frontmatter and body**

```markdown
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
  - ast.ts-refactor.rename_symbol
  - ast.ts-refactor.find_references
  - ast.ts-refactor.move_symbol
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

### `ast.ts-refactor.rename_symbol`

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

### `ast.ts-refactor.find_references`

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

### `ast.ts-refactor.move_symbol`

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
```

---

## Task 8: Vitest Unit Tests

**Files:**
- Create: `tests/refactor.test.ts`

- [ ] **Step 1: Create `tests/refactor.test.ts` with fixture helpers and setup**

Each test writes a small TypeScript project to a fresh temp directory (with its own `tsconfig.json`) and instantiates `TsRefactor`. The fixture deliberately includes a barrel re-export to exercise cross-file resolution.

```typescript
// tests/refactor.test.ts
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { TsRefactor } from "../services/skills/ast-ts-refactor/src/refactor.js";

let tmp: string;

function write(rel: string, content: string): void {
  const full = path.join(tmp, rel);
  fs.mkdirSync(path.dirname(full), { recursive: true });
  fs.writeFileSync(full, content, "utf-8");
}

beforeEach(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), "ts-refactor-"));
  write(
    "tsconfig.json",
    JSON.stringify({ compilerOptions: { target: "ES2022", module: "NodeNext", strict: true }, include: ["src"] }),
  );
  // Declaration
  write("src/order.ts", `export function computeTotal(n: number): number {\n  return n * 2;\n}\n`);
  // Direct consumer
  write(
    "src/checkout.ts",
    `import { computeTotal } from "./order.js";\nexport const price = computeTotal(10);\n`,
  );
  // Barrel re-export
  write("src/index.ts", `export { computeTotal } from "./order.js";\n`);
  // Consumer via barrel
  write(
    "src/report.ts",
    `import { computeTotal } from "./index.js";\nexport const r = computeTotal(5);\n`,
  );
});

afterEach(() => {
  fs.rmSync(tmp, { recursive: true, force: true });
  vi.restoreAllMocks();
});
```

- [ ] **Step 2: Test `renameSymbol` updates all files (incl. barrel) and does NOT auto-save**

```typescript
describe("renameSymbol", () => {
  it("renames across files including barrel re-export and returns a diff without saving", () => {
    const r = new TsRefactor();
    const tsconfig = path.join(tmp, "tsconfig.json");
    const diff = r.renameSymbol(tsconfig, path.join(tmp, "src/order.ts"), "computeTotal", "computeOrderTotal");

    expect(diff.changes).toBeGreaterThan(0);
    // All four files reference or declare the symbol.
    const affected = diff.files_affected.map((f) => path.basename(f)).sort();
    expect(affected).toContain("order.ts");
    expect(affected).toContain("checkout.ts");
    expect(affected).toContain("index.ts");
    expect(affected).toContain("report.ts");
    expect(diff.unified_diff).toContain("computeOrderTotal");

    // NOT auto-saved: on-disk files still contain the old name.
    const onDisk = fs.readFileSync(path.join(tmp, "src/order.ts"), "utf-8");
    expect(onDisk).toContain("computeTotal");
    expect(onDisk).not.toContain("computeOrderTotal");
  });
});
```

- [ ] **Step 3: Test `findReferences` returns all usage sites including the barrel import**

```typescript
describe("findReferences", () => {
  it("returns all usage sites including the barrel re-export", () => {
    const r = new TsRefactor();
    const tsconfig = path.join(tmp, "tsconfig.json");
    const refs = r.findReferences(tsconfig, path.join(tmp, "src/order.ts"), "computeTotal");

    const files = new Set(refs.map((ref) => path.basename(ref.file)));
    expect(files.has("order.ts")).toBe(true);     // definition
    expect(files.has("checkout.ts")).toBe(true);  // direct import
    expect(files.has("index.ts")).toBe(true);     // barrel re-export
    expect(files.has("report.ts")).toBe(true);    // import via barrel

    expect(refs.some((ref) => ref.is_definition)).toBe(true);
    for (const ref of refs) {
      expect(ref.line).toBeGreaterThan(0);
      expect(ref.column).toBeGreaterThan(0);
      expect(typeof ref.text).toBe("string");
    }
  });
});
```

- [ ] **Step 4: Test `moveSymbol` moves the symbol and rewrites imports**

```typescript
describe("moveSymbol", () => {
  it("moves the symbol to a new file and rewrites imports", () => {
    const r = new TsRefactor();
    const tsconfig = path.join(tmp, "tsconfig.json");
    const diff = r.moveSymbol(
      tsconfig,
      path.join(tmp, "src/order.ts"),
      "computeTotal",
      path.join(tmp, "src/totals.ts"),
    );

    expect(diff.changes).toBeGreaterThan(0);
    const affected = diff.files_affected.map((f) => path.basename(f));
    expect(affected).toContain("totals.ts"); // destination created
    expect(affected).toContain("order.ts");  // source lost the declaration
    // The destination diff contains the moved declaration.
    expect(diff.unified_diff).toContain("computeTotal");

    // NOT auto-saved.
    expect(fs.existsSync(path.join(tmp, "src/totals.ts"))).toBe(false);
  });
});
```

- [ ] **Step 5: Test that a relative tsconfig path throws**

```typescript
describe("absolute path enforcement", () => {
  it("throws when tsconfig is a relative path", () => {
    const r = new TsRefactor();
    expect(() => r.findReferences("tsconfig.json", "src/order.ts", "computeTotal")).toThrow(
      /absolute path/i,
    );
  });
});
```

- [ ] **Step 6: Test that `console.log` is never called**

Spy on `console.log` and assert zero calls across a representative operation; assert `console.error` IS used for logging.

```typescript
describe("stdout hygiene", () => {
  it("never calls console.log; uses console.error for logging", () => {
    const logSpy = vi.spyOn(console, "log").mockImplementation(() => {});
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    const r = new TsRefactor();
    const tsconfig = path.join(tmp, "tsconfig.json");
    r.findReferences(tsconfig, path.join(tmp, "src/order.ts"), "computeTotal");
    r.renameSymbol(tsconfig, path.join(tmp, "src/order.ts"), "computeTotal", "computeOrderTotal");

    expect(logSpy).toHaveBeenCalledTimes(0);
    expect(errSpy.mock.calls.length).toBeGreaterThan(0);
  });
});
```

- [ ] **Step 7: Add a vitest config so tests resolve TS sources**

Create `services/skills/ast-ts-refactor/vitest.config.ts`:

```typescript
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["../../../tests/refactor.test.ts"],
    environment: "node",
  },
});
```

> Note: vitest transpiles TS sources on the fly via esbuild, so the `.js` import specifiers in the test (matching NodeNext ESM convention) resolve to the `.ts` sources without a prior `tsc` build.

---

## Task 9: Build and Verify

**Files:**
- None (verification only)

- [ ] **Step 1: Type-check and compile**

```bash
cd services/skills/ast-ts-refactor && npm run build
```

Expect a clean `dist/` with `index.js`, `refactor.js`, `types.js`.

- [ ] **Step 2: Run the test suite**

```bash
cd services/skills/ast-ts-refactor && npm test
```

Expect all tests in Task 8 to pass (rename incl. barrel, find_references incl. barrel, move + import rewrite, relative-path throw, zero `console.log`).

- [ ] **Step 3: Smoke-test the stdio server manually**

Send a `tools/list` request and confirm the response is a single line of valid JSON-RPC on stdout with no banner/log noise preceding it (logs must appear on stderr only).

```bash
cd services/skills/ast-ts-refactor && \
  printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  | node dist/index.js 2>/tmp/ts-refactor.stderr
```

Confirm: stdout is pure JSON-RPC (the three tools listed); `/tmp/ts-refactor.stderr` contains the `[ts-refactor] MCP server ready on stdio` line. Any non-JSON byte on stdout is a stdout-pollution bug — fix before completing.

---

## Self-Review Checklist (run after implementing)

- [ ] All three tools from spec 6.4 implemented with exact signatures: `rename_symbol`, `find_references`, `move_symbol`.
- [ ] `tsconfig` absolute-path enforcement present in `getProject` and tested.
- [ ] No `project.save()` call anywhere — edits stay in memory; diff is the return value; tested via on-disk assertions.
- [ ] `Diff` and `Reference` interfaces used consistently across `types.ts`, `refactor.ts`, `index.ts`, and tests.
- [ ] Cross-file resolution through re-exports/barrels exercised by the fixture (`index.ts` barrel) in rename and find_references tests.
- [ ] Zero `console.log` anywhere; all logging via `console.error`; asserted by a spy test.
- [ ] `inputSchema` for every tool is self-contained JSON Schema (no `$ref`).
- [ ] Errors returned as `isError: true` content, never thrown across the transport.
