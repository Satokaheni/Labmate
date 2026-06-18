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
