import { z } from 'zod';
export const ExecRunInput = z.object({
    command: z.string().describe('Shell command to execute.'),
    cwd: z.string().describe('Absolute working directory for the command.'),
    timeout: z.number().int().min(1).max(60_000).default(10_000)
        .describe('Timeout in milliseconds before the command is killed.'),
}).strict();
//# sourceMappingURL=exec.js.map