export interface TruncateResult {
    text: string;
    has_more: boolean;
    next_offset: number | null;
    total: number;
}
export declare function truncate(text: string, offset?: number, limit?: number): TruncateResult;
//# sourceMappingURL=truncate.d.ts.map