import { spawn } from 'node:child_process';
import { ExecRunInput } from '../schemas/exec.js';
import { truncate } from '../utils/truncate.js';
import { formatError } from '../utils/formatError.js';
import { log } from '../services/logger.js';
// Relaxed command validation: non-empty and under 8192 chars
const COMMAND_VALID = /^.{1,8192}$/s;
export async function makeExecRunHandler(args, extra) {
    // Basic validation: non-empty, reasonable length
    if (!COMMAND_VALID.test(args.command)) {
        return {
            content: [{ type: 'text', text: 'exec_run: command must be non-empty and under 8192 characters' }],
            isError: true,
        };
    }
    try {
        const result = await new Promise((resolve, reject) => {
            const proc = spawn('bash', ['-lc', args.command], {
                cwd: args.cwd,
                env: process.env,
            });
            const stdoutChunks = [];
            const stderrChunks = [];
            proc.stdout.on('data', (chunk) => stdoutChunks.push(chunk));
            proc.stderr.on('data', (chunk) => stderrChunks.push(chunk));
            const timer = setTimeout(() => {
                proc.kill('SIGTERM');
                reject(new Error(`exec_run timed out after ${args.timeout}ms`));
            }, args.timeout);
            proc.on('close', (code) => {
                clearTimeout(timer);
                resolve({
                    stdout: Buffer.concat(stdoutChunks).toString('utf8'),
                    stderr: Buffer.concat(stderrChunks).toString('utf8'),
                    exit_code: code ?? 1,
                });
            });
            proc.on('error', (err) => {
                clearTimeout(timer);
                reject(err);
            });
        });
        const ok = result.exit_code === 0;
        const { text: stdout } = truncate(result.stdout);
        const { text: stderr } = truncate(result.stderr);
        return {
            content: [{ type: 'text', text: JSON.stringify({ stdout, stderr, exit_code: result.exit_code, ok }) }],
            isError: !ok,
        };
    }
    catch (err) {
        log.error({ err, command: args.command }, 'exec_run failed');
        return { content: [{ type: 'text', text: formatError(err, { command: args.command, cwd: args.cwd }) }], isError: true };
    }
}
export function registerExecTools(server) {
    server.registerTool('exec_run', {
        description: 'Execute a shell command and return its output. Commands are executed through a login shell (bash -lc).',
        inputSchema: ExecRunInput.shape,
        annotations: { readOnlyHint: false, openWorldHint: true },
    }, makeExecRunHandler);
}
//# sourceMappingURL=exec.js.map