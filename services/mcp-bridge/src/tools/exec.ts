import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import type { RequestHandlerExtra } from '@modelcontextprotocol/sdk/shared/protocol.js';
import type { ServerRequest, ServerNotification } from '@modelcontextprotocol/sdk/types.js';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { ExecRunInput } from '../schemas/exec.js';
import { truncate } from '../utils/truncate.js';
import { formatError } from '../utils/formatError.js';
import { log } from '../services/logger.js';

const execFileAsync = promisify(execFile);
const SHELL_METACHAR = /[;&|`$<>()\n\\]/;

export async function makeExecRunHandler(
  args: ExecRunInput,
  extra: RequestHandlerExtra<ServerRequest, ServerNotification>,
): Promise<{ content: { type: 'text'; text: string }[]; isError?: boolean }> {
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
  } catch (err) {
    log.error({ err, command: args.command }, 'exec_run failed');
    return { content: [{ type: 'text', text: formatError(err, { command: args.command, cwd: args.cwd }) }], isError: true };
  }
}

export function registerExecTools(server: McpServer): void {
  server.registerTool(
    'exec_run',
    {
      description: 'Execute a shell command and return its output. Shell metacharacters are rejected.',
      inputSchema: ExecRunInput.shape,
      annotations: { readOnlyHint: false, openWorldHint: true },
    },
    makeExecRunHandler,
  );
}
