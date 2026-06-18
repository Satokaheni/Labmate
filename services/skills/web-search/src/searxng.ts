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
