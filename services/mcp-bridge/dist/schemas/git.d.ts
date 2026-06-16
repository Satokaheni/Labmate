import { z } from 'zod';
export declare const GitLogInput: z.ZodObject<{
    repo_path: z.ZodString;
    max_count: z.ZodDefault<z.ZodNumber>;
    branch: z.ZodOptional<z.ZodString>;
}, "strict", z.ZodTypeAny, {
    repo_path: string;
    max_count: number;
    branch?: string | undefined;
}, {
    repo_path: string;
    max_count?: number | undefined;
    branch?: string | undefined;
}>;
export declare const GitStatusInput: z.ZodObject<{
    repo_path: z.ZodString;
}, "strict", z.ZodTypeAny, {
    repo_path: string;
}, {
    repo_path: string;
}>;
export declare const GitDiffInput: z.ZodObject<{
    repo_path: z.ZodString;
    ref_a: z.ZodDefault<z.ZodString>;
    ref_b: z.ZodOptional<z.ZodString>;
}, "strict", z.ZodTypeAny, {
    repo_path: string;
    ref_a: string;
    ref_b?: string | undefined;
}, {
    repo_path: string;
    ref_a?: string | undefined;
    ref_b?: string | undefined;
}>;
export type GitLogInput = z.infer<typeof GitLogInput>;
export type GitStatusInput = z.infer<typeof GitStatusInput>;
export type GitDiffInput = z.infer<typeof GitDiffInput>;
//# sourceMappingURL=git.d.ts.map