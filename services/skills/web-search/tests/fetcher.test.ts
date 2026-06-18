import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("node-fetch", () => ({ default: vi.fn() }));
import fetch from "node-fetch";
import { PageFetcher } from "../src/fetcher.js";

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
