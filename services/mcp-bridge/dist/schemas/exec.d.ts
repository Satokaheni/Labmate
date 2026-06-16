import { z } from 'zod';
export declare const ExecRunInput: z.ZodObject<{
    command: z.ZodString;
    cwd: z.ZodString;
    timeout: z.ZodDefault<z.ZodNumber>;
}, "strict", z.ZodTypeAny, {
    command: string;
    cwd: string;
    timeout: number;
}, {
    command: string;
    cwd: string;
    timeout?: number | undefined;
}>;
export type ExecRunInput = z.infer<typeof ExecRunInput>;
//# sourceMappingURL=exec.d.ts.map