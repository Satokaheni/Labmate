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
