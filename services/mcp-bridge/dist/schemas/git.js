import { z } from 'zod';
export const GitLogInput = z.object({
    repo_path: z.string().describe('Absolute path to the git repository root.'),
    max_count: z.number().int().min(1).max(200).default(20)
        .describe('Maximum number of commits to return.'),
    branch: z.string().optional().describe('Branch name; defaults to HEAD.'),
}).strict();
export const GitStatusInput = z.object({
    repo_path: z.string().describe('Absolute path to the git repository root.'),
}).strict();
export const GitDiffInput = z.object({
    repo_path: z.string().describe('Absolute path to the git repository root.'),
    ref_a: z.string().default('HEAD').describe('First ref (commit, branch, tag).'),
    ref_b: z.string().optional().describe('Second ref; omit to diff working tree against ref_a.'),
}).strict();
//# sourceMappingURL=git.js.map