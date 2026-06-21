import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import type { RequestHandlerExtra } from '@modelcontextprotocol/sdk/shared/protocol.js';
import type { ServerRequest, ServerNotification } from '@modelcontextprotocol/sdk/types.js';
import { spawn } from 'node:child_process';
import { ExecRunInput } from '../schemas/exec.js';
import { truncate } from '../utils/truncate.js';
import { formatError } from '../utils/formatError.js';
import { log } from '../services/logger.js';

// Relaxed command validation: non-empty and under 8192 chars
const COMMAND_VALID = /^.{1,8192}$/s;

// Sandbox bypass patterns: code execution that must go through code-sandbox skill
const SANDBOX_BYPASS_PATTERNS: RegExp[] = [
  /\bpython3?\s+-c\b/,        // python -c '...'
  /\bpython3?\s+\S+\.py\b/,   // python foo.py
  /\bnode\s+-e\b/,            // node -e '...'
  /\bnode\s+\S+\.js\b/,       // node foo.js
  /\bbash\s+\S+\.sh\b/,       // bash foo.sh
  /\bpytest\b/,               // pytest ...
  /\beval\b/,                 // eval "..."
  /\bcurl\b.*\|.*\bsh\b/,     // curl ... | sh
];

export function guardRunBash(cmd: string): string | null {
  for (const pattern of SANDBOX_BYPASS_PATTERNS) {
    if (pattern.test(cmd)) {
      return (
        'exec_run: this command looks like code execution and is not allowed ' +
        'through the generic shell. Run agent-generated code via the ' +
        'code-sandbox skill (isolated container) instead.'
      );
    }
  }
  return null;
}

export async function makeExecRunHandler(
  args: ExecRunInput,
  extra: RequestHandlerExtra<ServerRequest, ServerNotification>,
): Promise<{ content: { type: 'text'; text: string }[]; isError?: boolean }> {
  // Basic validation: non-empty, reasonable length
  if (!COMMAND_VALID.test(args.command)) {
    return {
      content: [{ type: 'text', text: 'exec_run: command must be non-empty and under 8192 characters' }],
      isError: true,
    };
  }

  const bypass = guardRunBash(args.command);
  if (bypass) {
    return {
      content: [{ type: 'text', text: bypass }],
      isError: true,
    };
  }

  try {
    const result = await new Promise<{ stdout: string; stderr: string; exit_code: number }>(
      (resolve, reject) => {
        const proc = spawn('bash', ['-lc', args.command], {
          cwd: args.cwd,
          env: process.env,
        });

        const stdoutChunks: Buffer[] = [];
        const stderrChunks: Buffer[] = [];

        proc.stdout!.on('data', (chunk: Buffer) => stdoutChunks.push(chunk));
        proc.stderr!.on('data', (chunk: Buffer) => stderrChunks.push(chunk));

        const timer = setTimeout(() => {
          proc.kill('SIGTERM');
          reject(new Error(`exec_run timed out after ${args.timeout}ms`));
        }, args.timeout);

        proc.on('close', (code: number | null) => {
          clearTimeout(timer);
          resolve({
            stdout: Buffer.concat(stdoutChunks).toString('utf8'),
            stderr: Buffer.concat(stderrChunks).toString('utf8'),
            exit_code: code ?? 1,
          });
        });

        proc.on('error', (err: Error) => {
          clearTimeout(timer);
          reject(err);
        });
      }
    );

    const ok = result.exit_code === 0;
    const { text: stdout } = truncate(result.stdout);
    const { text: stderr } = truncate(result.stderr);
    return {
      content: [{ type: 'text', text: JSON.stringify({ stdout, stderr, exit_code: result.exit_code, ok }) }],
      isError: !ok,
    };
  } catch (err) {
    log.error({ err, command: args.command }, 'exec_run failed');
    return { content: [{ type: 'text', text: formatError(err, { command: args.command, cwd: args.cwd }) }], isError: true };
  }
}

export function registerExecTools(server: McpServer): void {
  server.registerTool(
    'exec_run',
    {
      description: 'Execute a shell command and return its output. Commands are executed through a login shell (bash -lc).',
      inputSchema: ExecRunInput.shape,
      annotations: { readOnlyHint: false, openWorldHint: true },
    },
    makeExecRunHandler,
  );
}
