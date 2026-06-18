import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// node-fetch is the default export used by searxng.ts.
vi.mock("node-fetch", () => ({ default: vi.fn() }));
import fetch from "node-fetch";
import { SearxngClient } from "../src/searxng.js";

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
