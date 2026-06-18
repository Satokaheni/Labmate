import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { SearxngClient } from "./searxng.js";
import { PageFetcher } from "./fetcher.js";
import type { SearchResult, ToolError } from "./types.js";

const searxng = new SearxngClient();
const fetcher = new PageFetcher();

function toJsonl(results: SearchResult[]): string {
  return results.map((r) => JSON.stringify(r)).join("\n");
}

function errorPayload(message: string, detail: unknown): string {
  const payload: ToolError = {
    error: message,
    detail: detail instanceof Error ? detail.message : String(detail),
  };
  return JSON.stringify(payload);
}

const server = new McpServer({
  name: "web-search",
  version: "0.1.0",
});

server.tool(
  "web_search.search",
  "Search the web via self-hosted SearXNG. Returns JSONL of results.",
  {
    query: z.string(),
    limit: z.number().int().positive().default(10),
    categories: z.array(z.string()).default(["general"]),
  },
  async ({ query, limit, categories }) => {
    try {
      const results = await searxng.search(query, limit, categories);
      return { content: [{ type: "text", text: toJsonl(results) }] };
    } catch (err) {
      console.error("[web_search.search] failed:", err);
      return {
        content: [{ type: "text", text: errorPayload("search_failed", err) }],
        isError: true,
      };
    }
  },
);

server.tool(
  "web_search.fetch_page",
  "Fetch a URL and extract its main text content. Returns JSON.",
  {
    url: z.string().url(),
    max_length: z.number().int().positive().default(8000),
  },
  async ({ url, max_length }) => {
    try {
      const result = await fetcher.fetch(url, max_length);
      return { content: [{ type: "text", text: JSON.stringify(result) }] };
    } catch (err) {
      console.error("[web_search.fetch_page] failed:", err);
      return {
        content: [{ type: "text", text: errorPayload("fetch_failed", err) }],
        isError: true,
      };
    }
  },
);

server.tool(
  "web_search.search_code",
  "Search for code (GitHub/StackOverflow) via SearXNG code category. Returns JSONL.",
  {
    query: z.string(),
    limit: z.number().int().positive().default(5),
  },
  async ({ query, limit }) => {
    try {
      const results = await searxng.searchCode(query, limit);
      return { content: [{ type: "text", text: toJsonl(results) }] };
    } catch (err) {
      console.error("[web_search.search_code] failed:", err);
      return {
        content: [{ type: "text", text: errorPayload("search_failed", err) }],
        isError: true,
      };
    }
  },
);

async function main(): Promise<void> {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("[web-search] MCP server started on stdio");
}

main().catch((err) => {
  console.error("[web-search] fatal:", err);
  process.exit(1);
});
