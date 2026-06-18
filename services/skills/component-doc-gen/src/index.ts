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

async function buildDoc(componentPath: string, includeStories: boolean): Promise<ComponentDoc> {
  const { componentName, props, filePath } = parser.extractProps(componentPath);
  const description = await enrichDescription(componentName, props); // "" unless GEMMA_BASE set
  return docgen.generate(componentName, filePath, props, includeStories, description);
}

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

async function main(): Promise<void> {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("[component-doc-gen] MCP server ready on stdio");
}

main().catch((err) => {
  console.error("[component-doc-gen] fatal:", err);
  process.exit(1);
});
