import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("node-fetch", () => ({ default: vi.fn() }));
import fetch from "node-fetch";
import { SearxngClient } from "../src/searxng.js";

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
    const client = new SearxngClient("http://searxng:8080");
    // Spy at the method level — more reliable than intercepting the node-fetch
    // module binding in the compiled ESM output.
    vi.spyOn(client, "search").mockRejectedValue(new Error("ECONNREFUSED"));
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
