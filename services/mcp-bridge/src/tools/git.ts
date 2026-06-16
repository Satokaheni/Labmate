import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { GitLogInput, GitStatusInput, GitDiffInput } from '../schemas/git.js';
import { truncate } from '../utils/truncate.js';
import { log } from '../services/logger.js';

const execFileAsync = promisify(execFile);

async function runGit(args: string[], cwd: string): Promise<string> {
  const { stdout } = await execFileAsync('git', args, { cwd, encoding: 'utf8' });
  return stdout;
}

export async function makeStatusHandler(
  args: GitStatusInput,
): Promise<{ content: { type: 'text'; text: string }[]; isError?: true }> {
  try {
    const out = await runGit(['status', '--short', '--branch'], args.repo_path);
    const { text } = truncate(out);
    return { content: [{ type: 'text', text }] };
  } catch (err) {
    log.error({ err }, 'git_status failed');
    const msg = err instanceof Error ? err.message : String(err);
    return { content: [{ type: 'text', text: `git status failed: ${msg}` }], isError: true };
  }
}

export async function makeLogHandler(
  args: GitLogInput,
): Promise<{ content: { type: 'text'; text: string }[]; isError?: true }> {
  try {
    const gitArgs = ['log', `--max-count=${args.max_count}`, '--oneline'];
    if (args.branch) gitArgs.push(args.branch);
    const out = await runGit(gitArgs, args.repo_path);
    const { text } = truncate(out);
    return { content: [{ type: 'text', text }] };
  } catch (err) {
    log.error({ err }, 'git_log failed');
    const msg = err instanceof Error ? err.message : String(err);
    return { content: [{ type: 'text', text: `git log failed: ${msg}` }], isError: true };
  }
}

export async function makeDiffHandler(
  args: GitDiffInput,
): Promise<{ content: { type: 'text'; text: string }[]; isError?: true }> {
  try {
    const gitArgs = args.ref_b
      ? ['diff', args.ref_a, args.ref_b]
      : ['diff', args.ref_a];
    const out = await runGit(gitArgs, args.repo_path);
    const { text } = truncate(out);
    return { content: [{ type: 'text', text: text || '(no diff)' }] };
  } catch (err) {
    log.error({ err }, 'git_diff failed');
    const msg = err instanceof Error ? err.message : String(err);
    return { content: [{ type: 'text', text: `git diff failed: ${msg}` }], isError: true };
  }
}

export function registerGitTools(server: McpServer): void {
  server.registerTool(
    'git_status',
    {
      title: 'Git status',
      description: 'Get the working tree status of a git repository.',
      inputSchema: GitStatusInput.shape,
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    makeStatusHandler,
  );

  server.registerTool(
    'git_log',
    {
      title: 'Git log',
      description: 'Get the recent commit history of a git repository.',
      inputSchema: GitLogInput.shape,
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    makeLogHandler,
  );

  server.registerTool(
    'git_diff',
    {
      title: 'Git diff',
      description: 'Show changes between two refs, or between a ref and the working tree.',
      inputSchema: GitDiffInput.shape,
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    makeDiffHandler,
  );
}
