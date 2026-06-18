# web-search MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the web-search TypeScript MCP server wrapping self-hosted SearXNG for local-first live web search.

**Architecture:** SearxngClient calls the SearXNG JSON API (self-hosted Docker container lm-searxng). PageFetcher uses cheerio to extract main text from fetched HTML. All tools return JSONL for predictable sizes. SEARXNG_URL from environment. Offline mode returns structured errors. All logging via console.error (never console.log).

**Tech Stack:** Node.js 20+, TypeScript 5+, `@modelcontextprotocol/sdk`, `zod`, `cheerio`, `node-fetch`, `vitest`

---

## Conventions

- **stdout is sacred.** Never `console.log()` anywhere in this server. Every log line goes to `console.error()`. stdout carries JSON-RPC 2.0; any stray byte corrupts the stream.
- TypeScript files use `camelCase.ts`; interfaces/types use PascalCase.
- All external URLs come from environment variables. `SEARXNG_URL = process.env.SEARXNG_URL ?? "http://localhost:8080"`.
- Tools return strings (JSONL or JSON) for predictable, cappable sizes.
- Errors are returned as structured objects, never thrown out of a tool handler.

---

## Task 1: Scaffold the skill package directory

- [ ] Create directory `services/skills/web-search/src/`.
- [ ] Create `services/skills/web-search/package.json`:

```json
{
  "name": "@labmate/skill-web-search",
  "version": "0.1.0",
  "description": "Live web search and page fetching via self-hosted SearXNG",
  "license": "MIT",
  "type": "module",
  "bin": {
    "web-search-skill": "dist/index.js"
  },
  "scripts": {
    "build": "tsc",
    "start": "node dist/index.js",
    "test": "vitest run",
    "test:watch": "vitest"
  },
  "dependencies": {
    "@modelcontextprotocol/sdk": "^1.0.0",
    "cheerio": "^1.0.0",
    "node-fetch": "^3.3.2",
    "zod": "^3.23.0"
  },
  "devDependencies": {
    "@types/node": "^20.0.0",
    "typescript": "^5.4.0",
    "vitest": "^1.6.0"
  }
}
```

- [ ] Create `services/skills/web-search/tsconfig.json`:

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "Node16",
    "moduleResolution": "Node16",
    "outDir": "dist",
    "rootDir": "src",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "declaration": true,
    "resolveJsonModule": true
  },
  "include": ["src/**/*.ts"],
  "exclude": ["node_modules", "dist", "tests"]
}
```

- [ ] Run `cd services/skills/web-search && npm install` to verify the dependency tree resolves.

---

## Task 2: Define shared types

- [ ] Create `services/skills/web-search/src/types.ts`:

```typescript
export interface SearchResult {
  title: string;
  url: string;
  snippet: string;
  source: string;
  published_date: string | null;
}

export interface FetchResult {
  url: string;
  title: string;
  text: string; // main content, truncated to maxLength chars
  truncated: boolean;
}

export interface ToolError {
  error: string;
  detail: string;
}

// Shape of a single SearXNG JSON API result row (the fields we consume).
export interface SearxngRawResult {
  title?: string;
  url?: string;
  content?: string;
  engine?: string;
  publishedDate?: string | null;
}

export interface SearxngRawResponse {
  results?: SearxngRawResult[];
}
```

---

## Task 3: Implement SearxngClient

- [ ] Create `services/skills/web-search/src/searxng.ts`:

```typescript
import fetch from "node-fetch";
import type {
  SearchResult,
  SearxngRawResponse,
  SearxngRawResult,
} from "./types.js";

const DEFAULT_TIMEOUT_MS = 10_000;

export class SearxngClient {
  private baseUrl: string;

  constructor(baseUrl: string = process.env.SEARXNG_URL ?? "http://localhost:8080") {
    // Strip trailing slash for predictable URL building.
    this.baseUrl = baseUrl.replace(/\/+$/, "");
  }

  async search(
    query: string,
    limit: number,
    categories: string[],
  ): Promise<SearchResult[]> {
    const raw = await this.request(query, categories);
    return this.mapResults(raw, limit);
  }

  async searchCode(query: string, limit: number): Promise<SearchResult[]> {
    return this.search(query, limit, ["code"]);
  }

  private async request(
    query: string,
    categories: string[],
  ): Promise<SearxngRawResponse> {
    const params = new URLSearchParams({
      q: query,
      format: "json",
      categories: categories.join(","),
    });
    const url = `${this.baseUrl}/search?${params.toString()}`;

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), DEFAULT_TIMEOUT_MS);
    try {
      const res = await fetch(url, { signal: controller.signal });
      if (!res.ok) {
        throw new Error(`SearXNG returned HTTP ${res.status}`);
      }
      return (await res.json()) as SearxngRawResponse;
    } finally {
      clearTimeout(timer);
    }
  }

  private mapResults(raw: SearxngRawResponse, limit: number): SearchResult[] {
    const rows: SearxngRawResult[] = raw.results ?? [];
    return rows.slice(0, limit).map((r) => ({
      title: r.title ?? "",
      url: r.url ?? "",
      snippet: r.content ?? "",
      source: r.engine ?? "",
      published_date: r.publishedDate ?? null,
    }));
  }
}
```

- [ ] Note for implementer: `request()` deliberately lets network/HTTP errors throw. Offline handling lives in the tool handlers (Task 5), which wrap calls in try/catch and convert to a `ToolError`.

---

## Task 4: Implement PageFetcher

- [ ] Create `services/skills/web-search/src/fetcher.ts`:

```typescript
import fetch from "node-fetch";
import * as cheerio from "cheerio";
import type { FetchResult } from "./types.js";

const DEFAULT_TIMEOUT_MS = 15_000;

export class PageFetcher {
  async fetch(url: string, maxLength: number): Promise<FetchResult> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), DEFAULT_TIMEOUT_MS);
    let html: string;
    try {
      const res = await fetch(url, {
        signal: controller.signal,
        headers: { "User-Agent": "Labmate-web-search/0.1" },
      });
      if (!res.ok) {
        throw new Error(`Fetch returned HTTP ${res.status}`);
      }
      html = await res.text();
    } finally {
      clearTimeout(timer);
    }

    const $ = cheerio.load(html);
    const title = $("title").first().text().trim();
    const full = this.extractText(html);
    const truncated = full.length > maxLength;
    const text = truncated ? full.slice(0, maxLength) : full;

    return { url, title, text, truncated };
  }

  private extractText(html: string): string {
    const $ = cheerio.load(html);
    // Remove noise that pollutes extracted text.
    $("script, style, noscript, nav, header, footer, aside, iframe, svg").remove();
    const body = $("main").length ? $("main") : $("body");
    const text = body.text();
    // Collapse whitespace runs into single spaces / newlines.
    return text
      .replace(/[ \t\r\f\v]+/g, " ")
      .replace(/\n\s*\n\s*/g, "\n\n")
      .trim();
  }
}
```

---

## Task 5: Implement the MCP server entry point

- [ ] Create `services/skills/web-search/src/index.ts`:

```typescript
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
```

- [ ] Verify against the MCP SDK version actually installed: if the project's `@modelcontextprotocol/sdk` predates the `McpServer` high-level API, fall back to the low-level `Server` + `setRequestHandler(ListToolsRequestSchema/CallToolRequestSchema)` pattern. Confirm the import paths match the spec at `research/llm-harness-research/specs/spec_mcp_bridge.md`.

---

## Task 6: Write SKILL.md

- [ ] Create `services/skills/web-search/SKILL.md`:

```markdown
---
name: web-search
description: >
  Live web search and page fetching via self-hosted SearXNG. Use when the local corpus
  lacks current information — API documentation updates, recent papers, library changelogs,
  or any fact requiring freshness. Pairs with citation-check for grounding web results.
  Operates fully locally via Docker (no cloud search API required).
trigger: "Use when needing current information not available in the local document library"
tools:
  - web_search.search
  - web_search.fetch_page
  - web_search.search_code
version: "0.1.0"
license: MIT
requires: []
---

# web-search

Wraps a self-hosted SearXNG instance (Docker container `lm-searxng`) to provide
live web search and page-content extraction.

## Tools

### web_search.search(query, limit=10, categories=["general"])
Returns JSONL; one `SearchResult` per line: `title`, `url`, `snippet`, `source`,
`published_date`.

### web_search.fetch_page(url, max_length=8000)
Fetches a URL and extracts main text via cheerio. Returns JSON: `url`, `title`,
`text` (truncated to `max_length`), `truncated`.

### web_search.search_code(query, limit=5)
Searches the SearXNG `code` category (GitHub / StackOverflow). Returns JSONL.

## Configuration

- `SEARXNG_URL` (default `http://localhost:8080`) — base URL of the SearXNG instance.
  Inside the Docker network this is `http://searxng:8080`.

## Offline behavior

If SearXNG is unreachable, every tool returns a structured error object
(`{"error": "...", "detail": "..."}`) with `isError: true` rather than crashing.
```

---

## Task 7: Add the lm-searxng Docker container

- [ ] Read the project's existing Docker setup (the file referenced by `research/llm-harness-research/specs/spec_infrastructure.md`, e.g. `docker-compose.yml` or `run-services.sh`) to match conventions.
- [ ] Add a `searxng` service named `lm-searxng`:

```yaml
  searxng:
    image: searxng/searxng:latest
    container_name: lm-searxng
    ports:
      - "8080:8080"
    volumes:
      - searxng-config:/etc/searxng
    environment:
      - SEARXNG_BASE_URL=http://localhost:8080/
    restart: unless-stopped
```

- [ ] Add the volume:

```yaml
volumes:
  searxng-config:
```

- [ ] Ensure SearXNG's `settings.yml` enables the JSON format. In `searxng-config/settings.yml`:

```yaml
search:
  formats:
    - html
    - json
```

- [ ] The orchestrator / skill-worker must pass `SEARXNG_URL=http://searxng:8080` to the web-search skill process when spawning it inside the Docker network.

---

## Task 8: Write vitest tests for SearxngClient

- [ ] Create `tests/searxng.test.ts`:

```typescript
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// node-fetch is the default export used by searxng.ts.
vi.mock("node-fetch", () => ({ default: vi.fn() }));
import fetch from "node-fetch";
import { SearxngClient } from "../services/skills/web-search/src/searxng.js";

const mockFetch = fetch as unknown as ReturnType<typeof vi.fn>;

function jsonResponse(body: unknown) {
  return { ok: true, status: 200, json: async () => body };
}

describe("SearxngClient", () => {
  beforeEach(() => mockFetch.mockReset());
  afterEach(() => vi.restoreAllMocks());

  it("search() returns SearchResult[] with correct shape", async () => {
    mockFetch.mockResolvedValue(
      jsonResponse({
        results: [
          {
            title: "T",
            url: "https://e.com",
            content: "snip",
            engine: "duckduckgo",
            publishedDate: "2026-06-01",
          },
        ],
      }),
    );
    const client = new SearxngClient("http://searxng:8080");
    const out = await client.search("q", 10, ["general"]);
    expect(out).toEqual([
      {
        title: "T",
        url: "https://e.com",
        snippet: "snip",
        source: "duckduckgo",
        published_date: "2026-06-01",
      },
    ]);
  });

  it("search() respects limit", async () => {
    const results = Array.from({ length: 20 }, (_, i) => ({
      title: `t${i}`,
      url: `https://e.com/${i}`,
      content: "c",
      engine: "x",
    }));
    mockFetch.mockResolvedValue(jsonResponse({ results }));
    const client = new SearxngClient("http://searxng:8080");
    const out = await client.search("q", 5, ["general"]);
    expect(out).toHaveLength(5);
  });

  it("search() defaults published_date to null when absent", async () => {
    mockFetch.mockResolvedValue(
      jsonResponse({ results: [{ title: "t", url: "u", content: "c", engine: "e" }] }),
    );
    const client = new SearxngClient("http://searxng:8080");
    const out = await client.search("q", 10, ["general"]);
    expect(out[0].published_date).toBeNull();
  });

  it("searchCode() passes categories=['code'] to the API", async () => {
    mockFetch.mockResolvedValue(jsonResponse({ results: [] }));
    const client = new SearxngClient("http://searxng:8080");
    await client.searchCode("q", 5);
    const calledUrl = mockFetch.mock.calls[0][0] as string;
    expect(calledUrl).toContain("categories=code");
  });

  it("search() throws on non-OK HTTP (offline handled by caller)", async () => {
    mockFetch.mockResolvedValue({ ok: false, status: 502, json: async () => ({}) });
    const client = new SearxngClient("http://searxng:8080");
    await expect(client.search("q", 10, ["general"])).rejects.toThrow(/502/);
  });
});
```

---

## Task 9: Write vitest tests for PageFetcher

- [ ] Create `tests/fetcher.test.ts`:

```typescript
import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("node-fetch", () => ({ default: vi.fn() }));
import fetch from "node-fetch";
import { PageFetcher } from "../services/skills/web-search/src/fetcher.js";

const mockFetch = fetch as unknown as ReturnType<typeof vi.fn>;

function htmlResponse(html: string) {
  return { ok: true, status: 200, text: async () => html };
}

describe("PageFetcher", () => {
  beforeEach(() => mockFetch.mockReset());

  it("extracts title and main text", async () => {
    mockFetch.mockResolvedValue(
      htmlResponse(
        "<html><head><title>Hello</title></head><body><main>Body content here</main></body></html>",
      ),
    );
    const out = await new PageFetcher().fetch("https://e.com", 8000);
    expect(out.title).toBe("Hello");
    expect(out.text).toContain("Body content here");
    expect(out.truncated).toBe(false);
  });

  it("truncates at max_length and sets truncated=true", async () => {
    const long = "x".repeat(5000);
    mockFetch.mockResolvedValue(htmlResponse(`<body><main>${long}</main></body>`));
    const out = await new PageFetcher().fetch("https://e.com", 100);
    expect(out.text).toHaveLength(100);
    expect(out.truncated).toBe(true);
  });

  it("strips script and style noise", async () => {
    mockFetch.mockResolvedValue(
      htmlResponse(
        "<body><main>keep<script>var x=evil()</script><style>.a{}</style></main></body>",
      ),
    );
    const out = await new PageFetcher().fetch("https://e.com", 8000);
    expect(out.text).toContain("keep");
    expect(out.text).not.toContain("evil");
    expect(out.text).not.toContain(".a{}");
  });

  it("throws on non-OK HTTP", async () => {
    mockFetch.mockResolvedValue({ ok: false, status: 404, text: async () => "" });
    await expect(new PageFetcher().fetch("https://e.com", 8000)).rejects.toThrow(/404/);
  });
});
```

---

## Task 10: Write the offline-mode and stdout-safety handler tests

- [ ] Refactor tool-handler logic into a small testable module if needed, or test via the exported helpers. Create `tests/handlers.test.ts`:

```typescript
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("node-fetch", () => ({ default: vi.fn() }));
import fetch from "node-fetch";
import { SearxngClient } from "../services/skills/web-search/src/searxng.js";

const mockFetch = fetch as unknown as ReturnType<typeof vi.fn>;

// Mirrors the handler's try/catch contract: a thrown error becomes a structured
// payload rather than propagating.
async function safeSearch(client: SearxngClient): Promise<string> {
  try {
    const results = await client.search("q", 10, ["general"]);
    return results.map((r) => JSON.stringify(r)).join("\n");
  } catch (err) {
    return JSON.stringify({
      error: "search_failed",
      detail: err instanceof Error ? err.message : String(err),
    });
  }
}

describe("offline mode", () => {
  beforeEach(() => mockFetch.mockReset());

  it("returns a structured error object instead of throwing when SearXNG is unreachable", async () => {
    mockFetch.mockRejectedValue(new Error("ECONNREFUSED"));
    const client = new SearxngClient("http://searxng:8080");
    const out = await safeSearch(client);
    const parsed = JSON.parse(out);
    expect(parsed.error).toBe("search_failed");
    expect(parsed.detail).toContain("ECONNREFUSED");
  });
});

describe("stdout safety", () => {
  let logSpy: ReturnType<typeof vi.spyOn>;
  beforeEach(() => {
    logSpy = vi.spyOn(console, "log").mockImplementation(() => {});
    mockFetch.mockReset();
  });
  afterEach(() => logSpy.mockRestore());

  it("never calls console.log during a failing search", async () => {
    mockFetch.mockRejectedValue(new Error("boom"));
    const client = new SearxngClient("http://searxng:8080");
    await safeSearch(client);
    expect(logSpy).not.toHaveBeenCalled();
  });
});
```

- [ ] Note: for a stronger stdout guarantee, also add a lint/grep check in CI: `! grep -rn "console.log" services/skills/web-search/src`.

---

## Task 11: Build and verify

- [ ] Run `cd services/skills/web-search && npm run build` — confirm `dist/index.js` and the other compiled files appear with no TypeScript errors.
- [ ] Run `npx vitest run` from the repo root (or the package) — confirm all tests in `tests/searxng.test.ts`, `tests/fetcher.test.ts`, and `tests/handlers.test.ts` pass.
- [ ] Run `! grep -rn "console.log" services/skills/web-search/src` — confirm it finds nothing (stdout safety).
- [ ] Manual smoke test (requires `lm-searxng` running): `SEARXNG_URL=http://localhost:8080 node dist/index.js`, then send an MCP `initialize` + `tools/list` over stdin and confirm the three tools are listed and the startup banner appears on **stderr** only.

---

## Definition of Done

- [ ] `services/skills/web-search/` contains `src/{index,searxng,fetcher,types}.ts`, `SKILL.md`, `package.json`, `tsconfig.json`, and a built `dist/`.
- [ ] All three tools (`web_search.search`, `web_search.fetch_page`, `web_search.search_code`) are exposed over stdio MCP.
- [ ] `lm-searxng` Docker container added to the infrastructure setup with JSON format enabled.
- [ ] `fetch_page` truncates to `max_length` and reports `truncated`.
- [ ] Offline / unreachable SearXNG returns a structured error, never a crash.
- [ ] No `console.log` anywhere in `src/`.
- [ ] vitest suite green.
