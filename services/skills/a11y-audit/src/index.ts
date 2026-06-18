// index.ts — MCP server entry point (stdio transport)
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { A11yAuditor } from "./auditor.js";
import type { AuditError } from "./types.js";

const auditFileSchema = {
  html_or_component_path: z.string().describe("Path to an HTML file or built component to audit"),
  rules: z.array(z.string()).optional().describe("Optional axe rule IDs to restrict the audit to"),
};

const auditUrlSchema = {
  url: z.string().url().describe("http(s) URL to audit"),
  rules: z.array(z.string()).optional().describe("Optional axe rule IDs to restrict the audit to"),
};

const auditor = new A11yAuditor();

const server = new McpServer({
  name: "a11y-audit",
  version: "0.1.0",
});

function errorResult(label: string, err: unknown): { content: { type: "text"; text: string }[] } {
  const payload: AuditError = {
    error: err instanceof Error ? err.message : String(err),
    url_or_path: label,
  };
  console.error(`[a11y-audit] error auditing ${label}: ${payload.error}`);
  return { content: [{ type: "text", text: JSON.stringify(payload) }] };
}

server.registerTool(
  "audit_file",
  {
    description:
      "Render a local HTML file or built component in headless Chromium, run axe-core, return a WCAG violation report.",
    inputSchema: auditFileSchema,
  },
  async ({ html_or_component_path, rules }) => {
    try {
      const result = await auditor.auditFile(html_or_component_path, rules);
      return { content: [{ type: "text", text: JSON.stringify(result) }] };
    } catch (err) {
      return errorResult(html_or_component_path, err);
    }
  }
);

server.registerTool(
  "audit_url",
  {
    description:
      "Navigate to an http(s) URL in headless Chromium, run axe-core, return a WCAG violation report.",
    inputSchema: auditUrlSchema,
  },
  async ({ url, rules }) => {
    try {
      const result = await auditor.auditUrl(url, rules);
      return { content: [{ type: "text", text: JSON.stringify(result) }] };
    } catch (err) {
      return errorResult(url, err);
    }
  }
);

server.registerTool(
  "list_rules",
  {
    description: "List all available axe-core rule IDs with description and WCAG level (A/AA/AAA).",
    inputSchema: {},
  },
  async () => {
    try {
      const rules = await auditor.listRules();
      return { content: [{ type: "text", text: JSON.stringify(rules) }] };
    } catch (err) {
      return errorResult("list_rules", err);
    }
  }
);

async function shutdown(signal: string): Promise<void> {
  console.error(`[a11y-audit] received ${signal}, shutting down`);
  try {
    await auditor.close();
  } finally {
    process.exit(0);
  }
}

process.on("SIGTERM", () => void shutdown("SIGTERM"));
process.on("SIGINT", () => void shutdown("SIGINT"));

async function main(): Promise<void> {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("[a11y-audit] MCP server ready on stdio");
}

main().catch((err) => {
  console.error("[a11y-audit] fatal:", err);
  process.exit(1);
});
