# component-doc-gen MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the component-doc-gen TypeScript MCP server — AST-based React component documentation generation (prop tables, markdown, Storybook CSF3 stories) using ts-morph.

**Architecture:** ComponentParser uses ts-morph to find the component's Props interface or type alias, extracts each property's name, type, required status, and JSDoc description. DocGenerator renders the PropDef list into a markdown table, a Storybook CSF3 story template, and a full markdown doc block. All deterministic — no LLM calls. Optional Gemma 4 pass for description enrichment behind GEMMA_BASE env var. All logging via console.error.

**Tech Stack:** Node.js 20+, TypeScript 5+, `@modelcontextprotocol/sdk`, `ts-morph`, `zod`, `glob`, `vitest`

---

## File Map

| File | Responsibility |
|---|---|
| `services/skills/component-doc-gen/package.json` | ESM module, deps, build/test scripts |
| `services/skills/component-doc-gen/tsconfig.json` | strict, ES2022, NodeNext, emit to `dist/` |
| `services/skills/component-doc-gen/.gitignore` | ignore `dist/`, `node_modules/` |
| `services/skills/component-doc-gen/src/types.ts` | `PropDef`, `ComponentDoc` interfaces |
| `services/skills/component-doc-gen/src/parser.ts` | `ComponentParser` class — ts-morph prop extraction |
| `services/skills/component-doc-gen/src/docgen.ts` | `DocGenerator` class — props → markdown + Storybook |
| `services/skills/component-doc-gen/src/index.ts` | MCP server entry point (stdio transport) |
| `services/skills/component-doc-gen/SKILL.md` | Skill discovery artifact + instructions |
| `services/skills/component-doc-gen/vitest.config.ts` | vitest config pointing at the `tests/` sources |
| `tests/parser.test.ts` | vitest unit tests for `ComponentParser` over temp fixtures |
| `tests/docgen.test.ts` | vitest unit tests for `DocGenerator` + batch + stdout hygiene |

---

## Tool / requirement coverage

| Requirement | Task |
|---|---|
| `component_doc.generate(component_path, include_stories=True) -> str` (JSON: props_table, story_code, markdown_doc) | Task 3, Task 4, Task 6 |
| `component_doc.generate_batch(dir_path, pattern="**/*.tsx") -> str` (JSONL) | Task 5, Task 6 |
| Props interface / type alias extraction (name, type, required, default, JSDoc) | Task 3 |
| markdown prop table | Task 4 |
| Storybook CSF3 story (default export + named export) | Task 4 |
| Optional Gemma 4 description enrichment behind `GEMMA_BASE` | Task 4 (guarded), Task 6 |
| `console.error` only, never `console.log` | Task 6 (all source); Task 8 (test) |
| ts-morph absolute path requirement | Task 3 (`loadFile`), Task 7 (test) |

---

## Task 1: Project Scaffold

**Files:**
- Create: `services/skills/component-doc-gen/package.json`
- Create: `services/skills/component-doc-gen/tsconfig.json`
- Create: `services/skills/component-doc-gen/.gitignore`

- [ ] **Step 1: Create the directory structure**

```bash
mkdir -p services/skills/component-doc-gen/src
mkdir -p tests
```

- [ ] **Step 2: Create `services/skills/component-doc-gen/package.json`**

```json
{
  "name": "@labmate/component-doc-gen",
  "version": "0.1.0",
  "description": "AST-based React component documentation, prop tables, and Storybook CSF3 story generation using ts-morph",
  "license": "MIT",
  "type": "module",
  "main": "dist/index.js",
  "bin": {
    "component-doc-gen": "dist/index.js"
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
    "glob": "^11.0.0",
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

- [ ] **Step 3: Create `services/skills/component-doc-gen/tsconfig.json`**

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

- [ ] **Step 4: Create `services/skills/component-doc-gen/.gitignore`**

```
node_modules/
dist/
*.tsbuildinfo
```

- [ ] **Step 5: Install dependencies**

```bash
cd services/skills/component-doc-gen && npm install
```

---

## Task 2: Type Definitions

**Files:**
- Create: `services/skills/component-doc-gen/src/types.ts`

- [ ] **Step 1: Create `src/types.ts` with the `PropDef` and `ComponentDoc` interfaces**

```typescript
// src/types.ts

/** A single prop of a React component, extracted from its Props interface/type. */
export interface PropDef {
  name: string;
  type: string;                  // TypeScript type string (e.g. "string", "() => void", "'sm' | 'lg'")
  required: boolean;
  default_value: string | null;  // literal default if discoverable, else null
  description: string;           // from JSDoc comment if present, else ""
}

/** Full generated documentation for one React component. */
export interface ComponentDoc {
  component_name: string;
  file_path: string;
  props: PropDef[];
  props_table: string;     // markdown table
  story_code: string;      // Storybook CSF3 story ("" when stories not requested)
  markdown_doc: string;    // full markdown documentation
}
```

---

## Task 3: ComponentParser — load file, locate component, extract props

**Files:**
- Create: `services/skills/component-doc-gen/src/parser.ts`

- [ ] **Step 1: Create `src/parser.ts` with imports and the class shell + `loadFile`**

`loadFile` enforces the absolute-path rule (ts-morph misinterprets relative paths) and adds the single `.tsx` source to an in-memory ts-morph `Project`. We use an ad-hoc `Project` per parse (no tsconfig needed — we only need the source AST and JSDoc, not full type resolution), with `useInMemoryFileSystem: false` so the real file is read.

```typescript
// src/parser.ts
// NEVER console.log — ALWAYS console.error
import * as path from "node:path";
import {
  Project,
  SourceFile,
  InterfaceDeclaration,
  TypeAliasDeclaration,
  PropertySignature,
  Node,
} from "ts-morph";
import type { PropDef } from "./types.js";

export class ComponentParser {
  /** Load a single .tsx/.ts file into a throwaway ts-morph project. */
  private loadFile(componentPath: string): SourceFile {
    // ts-morph requires absolute paths; relative paths silently load the wrong file.
    if (!path.isAbsolute(componentPath)) {
      throw new Error(`component_path must be an absolute path, got: ${componentPath}`);
    }
    const project = new Project({
      compilerOptions: { allowJs: true, jsx: 4 /* ts.JsxEmit.ReactJSX */ },
      skipAddingFilesFromTsConfig: true,
    });
    console.error(`[component-doc-gen] loading ${componentPath}`);
    return project.addSourceFileAtPath(componentPath);
  }
}
```

- [ ] **Step 2: Add `componentName` — derive the component name from the file**

Prefers the default-exported function/const; falls back to the file basename (PascalCased).

```typescript
  /** Best-effort component name: default export name, else PascalCased file basename. */
  private componentName(sf: SourceFile): string {
    const defaultExport = sf.getDefaultExportSymbol();
    if (defaultExport) {
      const decls = defaultExport.getDeclarations();
      for (const d of decls) {
        const named = (d as unknown as { getName?: () => string }).getName?.();
        if (named && named !== "default") return named;
      }
    }
    // Fall back to the first exported function/variable that looks like a component.
    for (const fn of sf.getFunctions()) {
      const name = fn.getName();
      if (name && /^[A-Z]/.test(name)) return name;
    }
    for (const v of sf.getVariableDeclarations()) {
      const name = v.getName();
      if (/^[A-Z]/.test(name)) return name;
    }
    const base = path.basename(sf.getFilePath()).replace(/\.[jt]sx?$/, "");
    return base.charAt(0).toUpperCase() + base.slice(1);
  }
```

- [ ] **Step 3: Add `findPropsContainer` — locate the Props interface or type alias**

Looks for a declaration whose name matches `<Component>Props`, then `Props`, then the first interface/type that ends in `Props`.

```typescript
  /** Find the interface or type alias that declares the component's props. */
  private findPropsContainer(
    sf: SourceFile,
    componentName: string,
  ): InterfaceDeclaration | TypeAliasDeclaration | undefined {
    const candidates = [`${componentName}Props`, "Props"];
    for (const name of candidates) {
      const iface = sf.getInterface(name);
      if (iface) return iface;
      const alias = sf.getTypeAlias(name);
      if (alias) return alias;
    }
    // Fallback: any interface/type alias whose name ends in "Props".
    const iface = sf.getInterfaces().find((i) => i.getName().endsWith("Props"));
    if (iface) return iface;
    const alias = sf.getTypeAliases().find((a) => a.getName().endsWith("Props"));
    return alias;
  }
```

- [ ] **Step 4: Add `propsFromSignatures` — turn property signatures into `PropDef[]`**

Reads the type text, the `?` optional flag, the JSDoc, and `@default` tag (if any) from each `PropertySignature`.

```typescript
  /** Convert a list of property signatures into PropDef records. */
  private propsFromSignatures(signatures: PropertySignature[]): PropDef[] {
    const props: PropDef[] = [];
    for (const sig of signatures) {
      const typeNode = sig.getTypeNode();
      const type = typeNode ? typeNode.getText() : sig.getType().getText(sig);
      const required = !sig.hasQuestionToken();

      // JSDoc description: prefer the first JSDoc comment block's description text.
      let description = "";
      let defaultValue: string | null = null;
      const docs = sig.getJsDocs();
      if (docs.length > 0) {
        description = docs[docs.length - 1].getDescription().trim();
        for (const doc of docs) {
          for (const tag of doc.getTags()) {
            if (tag.getTagName() === "default" || tag.getTagName() === "defaultValue") {
              defaultValue = (tag.getCommentText() ?? "").trim() || null;
            }
          }
        }
      }

      props.push({
        name: sig.getName(),
        type: type.replace(/\s+/g, " ").trim(),
        required,
        default_value: defaultValue,
        description,
      });
    }
    return props;
  }
```

- [ ] **Step 5: Add the public `extractProps` method**

Resolves the container to a list of `PropertySignature` nodes (interface members, or the members of a type-literal alias), then delegates to `propsFromSignatures`. Returns `[]` when no Props container is found (a valid component with no props).

```typescript
  /** Extract the component name and its props from a single source file. */
  extractProps(componentPath: string): { componentName: string; props: PropDef[]; filePath: string } {
    const sf = this.loadFile(componentPath);
    const componentName = this.componentName(sf);
    const container = this.findPropsContainer(sf, componentName);

    let signatures: PropertySignature[] = [];
    if (container instanceof InterfaceDeclaration) {
      signatures = container.getProperties();
    } else if (container instanceof TypeAliasDeclaration) {
      const typeNode = container.getTypeNode();
      if (typeNode && Node.isTypeLiteral(typeNode)) {
        signatures = typeNode.getProperties();
      }
    }

    const props = this.propsFromSignatures(signatures);
    console.error(`[component-doc-gen] '${componentName}': ${props.length} props`);
    return { componentName, props, filePath: sf.getFilePath() };
  }
```

---

## Task 4: DocGenerator — markdown table, Storybook story, full doc

**Files:**
- Create: `services/skills/component-doc-gen/src/docgen.ts`

- [ ] **Step 1: Create `src/docgen.ts` with imports and the `renderPropsTable` method**

The table columns are: Prop, Type, Required, Default, Description. Pipe characters in type strings are escaped so union types render correctly inside the markdown table.

```typescript
// src/docgen.ts
// NEVER console.log — ALWAYS console.error
import type { PropDef, ComponentDoc } from "./types.js";

export class DocGenerator {
  /** Render a PropDef[] as a GitHub-flavored markdown table. */
  renderPropsTable(props: PropDef[]): string {
    const header =
      "| Prop | Type | Required | Default | Description |\n" +
      "| --- | --- | --- | --- | --- |";
    if (props.length === 0) {
      return `${header}\n| _(none)_ | | | | |`;
    }
    const rows = props.map((p) => {
      const type = p.type.replace(/\|/g, "\\|");
      const required = p.required ? "yes" : "no";
      const def = p.default_value === null ? "" : p.default_value.replace(/\|/g, "\\|");
      const desc = p.description.replace(/\|/g, "\\|").replace(/\n/g, " ");
      return `| \`${p.name}\` | \`${type}\` | ${required} | ${def} | ${desc} |`;
    });
    return [header, ...rows].join("\n");
  }
}
```

- [ ] **Step 2: Add `sampleValue` — produce a plausible default arg value per prop type**

Used to populate the Storybook story `args`. Deterministic, best-effort mapping from a TS type string to a literal.

```typescript
  /** Best-effort literal sample value for a prop, used in story args. */
  private sampleValue(prop: PropDef): string {
    if (prop.default_value !== null) return prop.default_value;
    const t = prop.type.replace(/\s+/g, "");
    if (/^['"`]/.test(t) || t.includes("|") && /['"]/.test(t)) {
      // Union of string literals — pick the first member.
      const first = t.split("|")[0];
      return first.startsWith("'") || first.startsWith('"') ? first : `'${first}'`;
    }
    if (t === "string") return "'text'";
    if (t === "number") return "0";
    if (t === "boolean") return "false";
    if (/\)=>|=>/.test(t) || t.includes("=>")) return "() => {}";
    if (t.endsWith("[]") || t.startsWith("Array<")) return "[]";
    if (t === "ReactNode" || t === "React.ReactNode") return "'children'";
    return "undefined";
  }
```

- [ ] **Step 3: Add `renderStory` — Storybook CSF3 story (default export + named export)**

CSF3 requires a default export of type `Meta` and at least one named export of type `StoryObj`. We emit one `Default` story populated with required-prop args.

```typescript
  /** Render a Storybook CSF3 story file for the component. */
  renderStory(componentName: string, props: PropDef[], importPath: string): string {
    const requiredArgs = props
      .filter((p) => p.required)
      .map((p) => `    ${p.name}: ${this.sampleValue(p)},`)
      .join("\n");
    const argsBlock = requiredArgs.length > 0 ? `\n  args: {\n${requiredArgs}\n  },\n` : "\n";

    return [
      `import type { Meta, StoryObj } from '@storybook/react';`,
      `import { ${componentName} } from '${importPath}';`,
      ``,
      `const meta: Meta<typeof ${componentName}> = {`,
      `  title: 'Components/${componentName}',`,
      `  component: ${componentName},`,
      `};`,
      ``,
      `export default meta;`,
      `type Story = StoryObj<typeof ${componentName}>;`,
      ``,
      `export const Default: Story = {${argsBlock}};`,
      ``,
    ].join("\n");
  }
```

- [ ] **Step 4: Add `renderMarkdownDoc` — full markdown documentation block**

```typescript
  /** Render the full markdown documentation for the component. */
  renderMarkdownDoc(componentName: string, propsTable: string, description: string): string {
    const intro = description.trim().length > 0 ? `\n${description.trim()}\n` : "";
    return [
      `# ${componentName}`,
      intro,
      `## Props`,
      ``,
      propsTable,
      ``,
    ].join("\n");
  }
```

- [ ] **Step 5: Add the public `generate` method tying it all together**

`importPath` is derived from the component file name (CSF3 stories import the component by relative module path). `includeStories=false` yields an empty `story_code`. The optional `description` argument carries an LLM-enriched intro when Gemma is enabled; otherwise it is `""`.

```typescript
  /** Assemble a ComponentDoc from extracted props. */
  generate(
    componentName: string,
    filePath: string,
    props: PropDef[],
    includeStories: boolean,
    description = "",
  ): ComponentDoc {
    const propsTable = this.renderPropsTable(props);
    const importPath = `./${filePath.replace(/^.*\//, "").replace(/\.[jt]sx?$/, "")}`;
    const storyCode = includeStories ? this.renderStory(componentName, props, importPath) : "";
    const markdownDoc = this.renderMarkdownDoc(componentName, propsTable, description);
    return {
      component_name: componentName,
      file_path: filePath,
      props,
      props_table: propsTable,
      story_code: storyCode,
      markdown_doc: markdownDoc,
    };
  }
}
```

---

## Task 5: Optional Gemma enrichment helper

**Files:**
- Create: `services/skills/component-doc-gen/src/enrich.ts`

The enrichment is fully optional and guarded: when `GEMMA_BASE` is unset, `enrichDescription` returns `""` immediately and no network call is made. This keeps the default path deterministic and offline.

- [ ] **Step 1: Create `src/enrich.ts`**

```typescript
// src/enrich.ts
// NEVER console.log — ALWAYS console.error
import type { PropDef } from "./types.js";

/**
 * Optionally ask Gemma 4 (OpenAI-compatible endpoint) to write a one-paragraph
 * human-readable description from the prop signatures. Returns "" when GEMMA_BASE
 * is unset or on any error — the AST output is always the source of truth.
 */
export async function enrichDescription(componentName: string, props: PropDef[]): Promise<string> {
  const base = process.env.GEMMA_BASE;
  if (!base) return ""; // disabled by default — deterministic, no network

  const signature = props
    .map((p) => `${p.name}${p.required ? "" : "?"}: ${p.type}`)
    .join("; ");
  const prompt =
    `Write a single concise paragraph describing the React component "${componentName}" ` +
    `based only on these props: ${signature}. Do not invent behavior not implied by the props.`;

  try {
    const res = await fetch(`${base.replace(/\/$/, "")}/v1/chat/completions`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        model: process.env.GEMMA_MODEL ?? "google/gemma-4-31B-it",
        messages: [{ role: "user", content: prompt }],
        max_tokens: 200,
        temperature: 0.2,
      }),
    });
    if (!res.ok) {
      console.error(`[component-doc-gen] enrichment HTTP ${res.status}; falling back to ""`);
      return "";
    }
    const data = (await res.json()) as { choices?: Array<{ message?: { content?: string } }> };
    return (data.choices?.[0]?.message?.content ?? "").trim();
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.error(`[component-doc-gen] enrichment failed: ${message}; falling back to ""`);
    return "";
  }
}
```

---

## Task 6: MCP Server Entry Point

**Files:**
- Create: `services/skills/component-doc-gen/src/index.ts`

- [ ] **Step 1: Create `src/index.ts` with imports, server, and zod input schemas**

```typescript
// src/index.ts
// NEVER console.log — ALWAYS console.error
import * as path from "node:path";
import { glob } from "glob";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";
import { ComponentParser } from "./parser.js";
import { DocGenerator } from "./docgen.js";
import { enrichDescription } from "./enrich.js";
import type { ComponentDoc } from "./types.js";

const parser = new ComponentParser();
const docgen = new DocGenerator();

const generateInput = z.object({
  component_path: z.string().describe("Absolute path to the React component .tsx file"),
  include_stories: z.boolean().default(true).describe("Also generate a Storybook CSF3 story"),
});

const batchInput = z.object({
  dir_path: z.string().describe("Absolute directory path to scan for components"),
  pattern: z.string().default("**/*.tsx").describe("Glob pattern for component files"),
});
```

- [ ] **Step 2: Add a shared `buildDoc` helper**

Runs parse → optional enrichment → generate. Centralizes the flow so `generate` and `generate_batch` share it.

```typescript
async function buildDoc(componentPath: string, includeStories: boolean): Promise<ComponentDoc> {
  const { componentName, props, filePath } = parser.extractProps(componentPath);
  const description = await enrichDescription(componentName, props); // "" unless GEMMA_BASE set
  return docgen.generate(componentName, filePath, props, includeStories, description);
}
```

- [ ] **Step 3: Add inline JSON Schemas for `tools/list`**

The MCP `inputSchema` must be self-contained JSON Schema (no `$ref`). Hand-inline it; keep the zod objects only for runtime `.parse`.

```typescript
const GENERATE_SCHEMA = {
  type: "object",
  properties: {
    component_path: { type: "string", description: "Absolute path to the React component .tsx file" },
    include_stories: { type: "boolean", description: "Also generate a Storybook CSF3 story", default: true },
  },
  required: ["component_path"],
  additionalProperties: false,
} as const;

const BATCH_SCHEMA = {
  type: "object",
  properties: {
    dir_path: { type: "string", description: "Absolute directory path to scan for components" },
    pattern: { type: "string", description: "Glob pattern for component files", default: "**/*.tsx" },
  },
  required: ["dir_path"],
  additionalProperties: false,
} as const;
```

- [ ] **Step 4: Construct the server and register `ListToolsRequestSchema`**

Tool names use the dotted MCP convention `component_doc.generate` / `component_doc.generate_batch`.

```typescript
const server = new Server(
  { name: "component-doc-gen", version: "0.1.0" },
  { capabilities: { tools: {} } },
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "component_doc.generate",
      description:
        "Generate a markdown prop table, full markdown doc, and optional Storybook CSF3 story for a single React component file. AST-based (ts-morph), deterministic. component_path must be absolute.",
      inputSchema: GENERATE_SCHEMA,
    },
    {
      name: "component_doc.generate_batch",
      description:
        "Generate documentation for every React component matching a glob pattern under a directory. Returns JSONL (one ComponentDoc JSON per line). dir_path must be absolute.",
      inputSchema: BATCH_SCHEMA,
    },
  ],
}));
```

- [ ] **Step 5: Register `CallToolRequestSchema` with the dispatch switch**

`generate` returns a single JSON object as text. `generate_batch` globs the directory, builds a doc per file, and joins the results as JSONL. Errors are returned as `isError: true`, never thrown across the transport. A single file failing inside a batch is recorded as an error line, not a fatal stop.

```typescript
server.setRequestHandler(CallToolRequestSchema, async (req) => {
  const { name, arguments: args } = req.params;
  try {
    switch (name) {
      case "component_doc.generate": {
        const a = generateInput.parse(args);
        const doc = await buildDoc(a.component_path, a.include_stories);
        return { content: [{ type: "text", text: JSON.stringify(doc, null, 2) }] };
      }
      case "component_doc.generate_batch": {
        const a = batchInput.parse(args);
        if (!path.isAbsolute(a.dir_path)) {
          throw new Error(`dir_path must be an absolute path, got: ${a.dir_path}`);
        }
        const files = await glob(a.pattern, { cwd: a.dir_path, absolute: true, nodir: true });
        console.error(`[component-doc-gen] batch: ${files.length} files match ${a.pattern}`);
        const lines: string[] = [];
        for (const file of files) {
          try {
            const doc = await buildDoc(file, true);
            lines.push(JSON.stringify(doc));
          } catch (err) {
            const message = err instanceof Error ? err.message : String(err);
            console.error(`[component-doc-gen] batch skip ${file}: ${message}`);
            lines.push(JSON.stringify({ file_path: file, error: message }));
          }
        }
        return { content: [{ type: "text", text: lines.join("\n") }] };
      }
      default:
        return { content: [{ type: "text", text: `Unknown tool: ${name}` }], isError: true };
    }
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.error(`[component-doc-gen] tool '${name}' failed: ${message}`);
    return { content: [{ type: "text", text: `Error: ${message}` }], isError: true };
  }
});
```

- [ ] **Step 6: Connect the stdio transport and start**

```typescript
async function main(): Promise<void> {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("[component-doc-gen] MCP server ready on stdio");
}

main().catch((err) => {
  console.error("[component-doc-gen] fatal:", err);
  process.exit(1);
});
```

---

## Task 7: SKILL.md

**Files:**
- Create: `services/skills/component-doc-gen/SKILL.md`

- [ ] **Step 1: Create `SKILL.md` with frontmatter and body**

```markdown
---
name: component-doc-gen
description: >
  Auto-generates TypeScript prop tables, markdown documentation, and Storybook
  CSF3 stories from React component source files using AST analysis (ts-morph).
  Deterministic — no LLM required. Use when documenting generated or existing
  React components. Extracts props, types, required/optional status, and JSDoc.
trigger: "Use when generating documentation or Storybook stories for React components"
tools:
  - component_doc.generate
  - component_doc.generate_batch
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

### `component_doc.generate`

Generate docs (and, by default, a Storybook story) for a single component file.

```json
{
  "component_path": "/abs/path/to/src/Button.tsx",
  "include_stories": true
}
```

Returns a JSON `ComponentDoc`:
`{ "component_name": "...", "file_path": "...", "props": [...], "props_table": "...", "story_code": "...", "markdown_doc": "..." }`.

### `component_doc.generate_batch`

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
```

---

## Task 8: Vitest Unit Tests

**Files:**
- Create: `tests/parser.test.ts`
- Create: `tests/docgen.test.ts`
- Create: `services/skills/component-doc-gen/vitest.config.ts`

- [ ] **Step 1: Create `services/skills/component-doc-gen/vitest.config.ts`**

```typescript
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["../../../tests/parser.test.ts", "../../../tests/docgen.test.ts"],
    environment: "node",
  },
});
```

> Note: vitest transpiles TS on the fly via esbuild, so the `.js` import specifiers in the tests (NodeNext ESM convention) resolve to the `.ts` sources without a prior `tsc` build.

- [ ] **Step 2: Create `tests/parser.test.ts` with fixture helpers and setup**

Each test writes a small `.tsx` file to a fresh temp directory and runs `ComponentParser` against its absolute path. The fixture exercises required vs. optional props, a union type, a JSDoc description, and a `@default` tag.

```typescript
// tests/parser.test.ts
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { ComponentParser } from "../services/skills/component-doc-gen/src/parser.js";

let tmp: string;

function write(rel: string, content: string): string {
  const full = path.join(tmp, rel);
  fs.mkdirSync(path.dirname(full), { recursive: true });
  fs.writeFileSync(full, content, "utf-8");
  return full;
}

beforeEach(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), "component-doc-"));
});

afterEach(() => {
  fs.rmSync(tmp, { recursive: true, force: true });
});

const BUTTON = `
import React from 'react';

export interface ButtonProps {
  /** The visible button label. */
  label: string;
  /** Visual size of the button.
   * @default 'md'
   */
  size?: 'sm' | 'md' | 'lg';
  /** Click handler. */
  onClick?: () => void;
  disabled?: boolean;
}

export function Button(props: ButtonProps) {
  return <button>{props.label}</button>;
}

export default Button;
`;
```

- [ ] **Step 3: Test prop extraction — names, types, required status**

```typescript
describe("ComponentParser.extractProps", () => {
  it("extracts prop names, types, and required status", () => {
    const file = write("Button.tsx", BUTTON);
    const parser = new ComponentParser();
    const { componentName, props } = parser.extractProps(file);

    expect(componentName).toBe("Button");
    const byName = Object.fromEntries(props.map((p) => [p.name, p]));

    expect(byName.label.required).toBe(true);
    expect(byName.label.type).toBe("string");

    expect(byName.size.required).toBe(false);
    expect(byName.size.type).toContain("'sm'");
    expect(byName.size.type).toContain("'lg'");

    expect(byName.onClick.required).toBe(false);
    expect(byName.disabled.required).toBe(false);
  });
});
```

- [ ] **Step 4: Test JSDoc descriptions and `@default` capture**

```typescript
describe("ComponentParser JSDoc", () => {
  it("captures JSDoc descriptions and @default values", () => {
    const file = write("Button.tsx", BUTTON);
    const parser = new ComponentParser();
    const { props } = parser.extractProps(file);
    const byName = Object.fromEntries(props.map((p) => [p.name, p]));

    expect(byName.label.description).toBe("The visible button label.");
    expect(byName.size.description).toContain("Visual size");
    expect(byName.size.default_value).toBe("'md'");
    expect(byName.disabled.description).toBe(""); // no JSDoc
  });

  it("supports a type-literal alias and a component with no props", () => {
    const parser = new ComponentParser();

    const aliasFile = write(
      "Card.tsx",
      `type CardProps = { title: string };\nexport function Card(p: CardProps) { return null; }\nexport default Card;\n`,
    );
    const alias = parser.extractProps(aliasFile);
    expect(alias.props.map((p) => p.name)).toEqual(["title"]);

    const noPropsFile = write(
      "Spacer.tsx",
      `export function Spacer() { return null; }\nexport default Spacer;\n`,
    );
    const none = parser.extractProps(noPropsFile);
    expect(none.props).toEqual([]);
  });

  it("throws on a relative component_path", () => {
    const parser = new ComponentParser();
    expect(() => parser.extractProps("Button.tsx")).toThrow(/absolute path/i);
  });
});
```

- [ ] **Step 5: Create `tests/docgen.test.ts` covering the table, story, batch shape, and stdout hygiene**

```typescript
// tests/docgen.test.ts
import { describe, it, expect, vi } from "vitest";
import { DocGenerator } from "../services/skills/component-doc-gen/src/docgen.js";
import type { PropDef } from "../services/skills/component-doc-gen/src/types.js";

const PROPS: PropDef[] = [
  { name: "label", type: "string", required: true, default_value: null, description: "The label." },
  { name: "size", type: "'sm' | 'md' | 'lg'", required: false, default_value: "'md'", description: "Size." },
  { name: "onClick", type: "() => void", required: false, default_value: null, description: "" },
];

describe("DocGenerator.renderPropsTable", () => {
  it("produces a markdown table with the correct columns and escaped unions", () => {
    const table = new DocGenerator().renderPropsTable(PROPS);
    const lines = table.split("\n");
    expect(lines[0]).toBe("| Prop | Type | Required | Default | Description |");
    expect(lines[1]).toBe("| --- | --- | --- | --- | --- |");
    expect(table).toContain("`label`");
    expect(table).toContain("yes");
    expect(table).toContain("no");
    expect(table).toContain("\\|"); // union pipe escaped
    expect(table).toContain("'md'"); // default rendered
  });

  it("handles a component with no props", () => {
    const table = new DocGenerator().renderPropsTable([]);
    expect(table).toContain("_(none)_");
  });
});

describe("DocGenerator.renderStory", () => {
  it("emits a CSF3 story with a default export and a named export", () => {
    const story = new DocGenerator().renderStory("Button", PROPS, "./Button");
    expect(story).toContain("import type { Meta, StoryObj } from '@storybook/react';");
    expect(story).toContain("import { Button } from './Button';");
    expect(story).toContain("export default meta;");          // CSF3 default export
    expect(story).toContain("export const Default: Story =");  // named export
    expect(story).toContain("label: 'text'");                 // required arg filled
    expect(story).not.toContain("onClick:");                  // optional arg omitted
  });
});

describe("DocGenerator.generate", () => {
  it("assembles a ComponentDoc and omits story_code when stories disabled", () => {
    const gen = new DocGenerator();
    const withStory = gen.generate("Button", "/abs/Button.tsx", PROPS, true);
    expect(withStory.story_code.length).toBeGreaterThan(0);
    expect(withStory.markdown_doc).toContain("# Button");
    expect(withStory.props_table).toContain("| Prop |");

    const noStory = gen.generate("Button", "/abs/Button.tsx", PROPS, false);
    expect(noStory.story_code).toBe("");
  });
});

describe("stdout hygiene", () => {
  it("never calls console.log; uses console.error for logging in the parser", async () => {
    const logSpy = vi.spyOn(console, "log").mockImplementation(() => {});
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    const fs = await import("node:fs");
    const os = await import("node:os");
    const path = await import("node:path");
    const { ComponentParser } = await import(
      "../services/skills/component-doc-gen/src/parser.js"
    );

    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "component-doc-log-"));
    const file = path.join(tmp, "Button.tsx");
    fs.writeFileSync(
      file,
      `export interface ButtonProps { label: string }\nexport function Button(p: ButtonProps){return null}\nexport default Button;\n`,
      "utf-8",
    );

    new ComponentParser().extractProps(file);

    expect(logSpy).toHaveBeenCalledTimes(0);
    expect(errSpy.mock.calls.length).toBeGreaterThan(0);

    fs.rmSync(tmp, { recursive: true, force: true });
    vi.restoreAllMocks();
  });
});
```

- [ ] **Step 6: Test `generate_batch` processes all matching files (integration via index dispatch)**

This exercises the glob + per-file loop. Import the parser/docgen directly and replicate the batch loop, or — preferred — extract `buildDoc` and the batch glob into a small testable function. For this plan, assert the loop over a two-fixture directory yields two JSONL lines.

```typescript
import { glob } from "glob";

describe("batch shape", () => {
  it("produces one JSONL line per matching .tsx file", async () => {
    const fs = await import("node:fs");
    const os = await import("node:os");
    const path = await import("node:path");
    const { ComponentParser } = await import(
      "../services/skills/component-doc-gen/src/parser.js"
    );

    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "component-doc-batch-"));
    for (const name of ["Button", "Card"]) {
      fs.writeFileSync(
        path.join(dir, `${name}.tsx`),
        `export interface ${name}Props { id: string }\nexport function ${name}(p: ${name}Props){return null}\nexport default ${name};\n`,
        "utf-8",
      );
    }

    const files = await glob("**/*.tsx", { cwd: dir, absolute: true, nodir: true });
    const parser = new ComponentParser();
    const gen = new DocGenerator();
    const lines = files.map((f) => {
      const { componentName, props, filePath } = parser.extractProps(f);
      return JSON.stringify(gen.generate(componentName, filePath, props, true));
    });

    expect(lines.length).toBe(2);
    for (const line of lines) {
      const doc = JSON.parse(line);
      expect(doc.component_name).toMatch(/Button|Card/);
      expect(Array.isArray(doc.props)).toBe(true);
    }

    fs.rmSync(dir, { recursive: true, force: true });
  });
});
```

---

## Task 9: Build and Verify

**Files:**
- None (verification only)

- [ ] **Step 1: Type-check and compile**

```bash
cd services/skills/component-doc-gen && npm run build
```

Expect a clean `dist/` with `index.js`, `parser.js`, `docgen.js`, `enrich.js`, `types.js`.

- [ ] **Step 2: Run the test suite**

```bash
cd services/skills/component-doc-gen && npm test
```

Expect all tests in Task 8 to pass (prop extraction, JSDoc + `@default`, type-literal alias, no-props, relative-path throw, markdown table columns, CSF3 story exports, batch shape, zero `console.log`).

- [ ] **Step 3: Smoke-test the stdio server manually**

Send a `tools/list` request and confirm the response is a single line of valid JSON-RPC on stdout with no banner/log noise preceding it (logs must appear on stderr only).

```bash
cd services/skills/component-doc-gen && \
  printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  | node dist/index.js 2>/tmp/component-doc-gen.stderr
```

Confirm: stdout is pure JSON-RPC (the two tools listed: `component_doc.generate`, `component_doc.generate_batch`); `/tmp/component-doc-gen.stderr` contains the `[component-doc-gen] MCP server ready on stdio` line. Any non-JSON byte on stdout is a stdout-pollution bug — fix before completing.

- [ ] **Step 4: Smoke-test `generate` against a real fixture**

```bash
cd services/skills/component-doc-gen && \
  printf 'export interface CardProps { /** Title */ title: string; subtitle?: string }\nexport function Card(p: CardProps){return null}\nexport default Card;\n' > /tmp/Card.tsx && \
  printf '%s\n' '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"component_doc.generate","arguments":{"component_path":"/tmp/Card.tsx","include_stories":true}}}' \
  | node dist/index.js 2>/dev/null
```

Confirm the returned `ComponentDoc` JSON has a `props_table` with `title`/`subtitle` rows, a `story_code` containing `export default meta;`, and a `markdown_doc` starting with `# Card`.

---

## Self-Review Checklist (run after implementing)

- [ ] Both tools implemented with exact names: `component_doc.generate`, `component_doc.generate_batch`.
- [ ] `generate` returns JSON with `props_table`, `story_code`, `markdown_doc`; `generate_batch` returns JSONL.
- [ ] `component_path` / `dir_path` absolute-path enforcement present and tested.
- [ ] Props extracted with name, type, required, default_value, description; JSDoc + `@default` captured.
- [ ] Storybook output is valid CSF3 (default `Meta` export + named `StoryObj` export); required args filled.
- [ ] `include_stories=false` yields empty `story_code`.
- [ ] Optional Gemma enrichment is guarded by `GEMMA_BASE`; default path makes no network call.
- [ ] `PropDef` and `ComponentDoc` interfaces used consistently across `types.ts`, `parser.ts`, `docgen.ts`, `index.ts`, and tests.
- [ ] Zero `console.log` anywhere; all logging via `console.error`; asserted by a spy test.
- [ ] `inputSchema` for every tool is self-contained JSON Schema (no `$ref`).
- [ ] Errors returned as `isError: true` content, never thrown across the transport; a single failing file does not abort a batch.
