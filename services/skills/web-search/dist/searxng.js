import fetch from "node-fetch";
const DEFAULT_TIMEOUT_MS = 10_000;
export class SearxngClient {
    baseUrl;
    constructor(baseUrl = process.env.SEARXNG_URL ?? "http://localhost:8080") {
        // Strip trailing slash for predictable URL building.
        this.baseUrl = baseUrl.replace(/\/+$/, "");
    }
    async search(query, limit, categories) {
        const raw = await this.request(query, categories);
        return this.mapResults(raw, limit);
    }
    async searchCode(query, limit) {
        return this.search(query, limit, ["code"]);
    }
    async request(query, categories) {
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
            return (await res.json());
        }
        finally {
            clearTimeout(timer);
        }
    }
    mapResults(raw, limit) {
        const rows = raw.results ?? [];
        return rows.slice(0, limit).map((r) => ({
            title: r.title ?? "",
            url: r.url ?? "",
            snippet: r.content ?? "",
            source: r.engine ?? "",
            published_date: r.publishedDate ?? null,
        }));
    }
}
