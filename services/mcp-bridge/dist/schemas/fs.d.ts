import { z } from 'zod';
export declare const FsReadInput: z.ZodObject<{
    path: z.ZodString;
    offset: z.ZodDefault<z.ZodNumber>;
    limit: z.ZodDefault<z.ZodNumber>;
}, "strict", z.ZodTypeAny, {
    path: string;
    offset: number;
    limit: number;
}, {
    path: string;
    offset?: number | undefined;
    limit?: number | undefined;
}>;
export declare const FsListInput: z.ZodObject<{
    path: z.ZodString;
    depth: z.ZodDefault<z.ZodNumber>;
}, "strict", z.ZodTypeAny, {
    path: string;
    depth: number;
}, {
    path: string;
    depth?: number | undefined;
}>;
export declare const FsWriteInput: z.ZodObject<{
    path: z.ZodString;
    content: z.ZodString;
}, "strict", z.ZodTypeAny, {
    path: string;
    content: string;
}, {
    path: string;
    content: string;
}>;
export type FsReadInput = z.infer<typeof FsReadInput>;
export type FsListInput = z.infer<typeof FsListInput>;
export type FsWriteInput = z.infer<typeof FsWriteInput>;
//# sourceMappingURL=fs.d.ts.map