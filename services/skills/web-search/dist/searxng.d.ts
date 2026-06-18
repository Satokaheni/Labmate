import type { SearchResult } from "./types.js";
export declare class SearxngClient {
    private baseUrl;
    constructor(baseUrl?: string);
    search(query: string, limit: number, categories: string[]): Promise<SearchResult[]>;
    searchCode(query: string, limit: number): Promise<SearchResult[]>;
    private request;
    private mapResults;
}
