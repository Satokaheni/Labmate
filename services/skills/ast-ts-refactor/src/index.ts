// src/index.ts
// NEVER console.log — ALWAYS console.error
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";
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

async function main(): Promise<void> {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("[ts-refactor] MCP server ready on stdio");
}

main().catch((err) => {
  console.error("[ts-refactor] fatal:", err);
  process.exit(1);
});
