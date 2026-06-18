import type { FetchResult } from "./types.js";
export declare class PageFetcher {
    fetch(url: string, maxLength: number): Promise<FetchResult>;
    private extractText;
}
