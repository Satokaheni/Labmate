import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { ExecRunInput } from '../schemas/exec.js';
import { truncate } from '../utils/truncate.js';
import { log } from '../services/logger.js';
const execFileAsync = promisify(execFile);
const SHELL_METACHAR = /[;&|`$<>()\n\\]/;
export async function makeExecRunHandler(args, extra) {
    if (SHELL_METACHAR.test(args.command)) {
        return {
            content: [{ type: 'text', text: 'exec_run: disallowed shell metacharacter in command' }],
            isError: true,
        };
    }
    const [cmd, ...cmdArgs] = args.command.split(/\s+/);
    try {
        const { stdout, stderr } = await execFileAsync(cmd, cmdArgs, {
            cwd: args.cwd,
            timeout: args.timeout,
            encoding: 'utf8',
        });
        const combined = [stdout, stderr].filter(Boolean).join('\n--- stderr ---\n');
        const { text } = truncate(combined);
        return { content: [{ type: 'text', text: text || '(no output)' }] };
    }
    catch (err) {
        log.error({ err, command: args.command }, 'exec_run failed');
        const msg = err instanceof Error ? err.message : String(err);
        return { content: [{ type: 'text', text: `exec_run failed: ${msg}` }], isError: true };
    }
}
export function registerExecTools(server) {
    server.registerTool('exec_run', {
        description: 'Execute a shell command and return its output. Shell metacharacters are rejected.',
        inputSchema: ExecRunInput.shape,
        annotations: { readOnlyHint: false, openWorldHint: true },
    }, makeExecRunHandler);
}
//# sourceMappingURL=exec.js.map