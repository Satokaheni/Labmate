import { promises as fs } from 'node:fs';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { resolveToolPath } from './workspace';

export type LocalToolName = 'read_file' | 'write_file' | 'list_dir' | 'search_files';
export const LOCAL_TOOL_NAMES: readonly LocalToolName[] = ['read_file', 'write_file', 'list_dir', 'search_files'];

export const execFileAsync = promisify(execFile);

/**
 * Get the ripgrep binary path. Checks LABMATE_RG_PATH env var, common install locations, or system 'rg'.
 * TODO(phase1-hardening): bundle rg via electron-builder extraFiles
 */
function ripgrepBin(): string {
  if (process.env.LABMATE_RG_PATH) {
    return process.env.LABMATE_RG_PATH;
  }

  // Check common brew install locations
  const commonPaths = [
    '/usr/local/bin/rg', // Intel Mac
    '/opt/homebrew/bin/rg', // Apple Silicon Mac
    '/usr/bin/rg',
  ];

  for (const p of commonPaths) {
    try {
      if (require('node:fs').statSync(p).isFile()) {
        return p;
      }
    } catch {
      // Path doesn't exist, try next
    }
  }

  // Fallback to system rg (PATH lookup)
  return 'rg';
}

interface SearchHit {
  file: string;
  line: number;
  text: string;
}

interface SearchResult {
  hits: SearchHit[];
  truncated: boolean;
}

/**
 * Execute a client-side file tool against the chat's workspace roots.
 * Relative paths resolve against the primary root (roots[0]); absolute paths
 * are allowed only inside one of the roots. See resolveToolPath.
 */
export async function executeTool(
  name: LocalToolName,
  args: Record<string, unknown>,
  roots: string[],
): Promise<unknown> {
  if (name === 'read_file') {
    const p = resolveToolPath(String(args.path ?? ''), roots);
    return { content: await fs.readFile(p, 'utf-8') };
  }
  if (name === 'write_file') {
    // Writes always target the primary root (roots[0]) for relative paths.
    const p = resolveToolPath(String(args.path ?? ''), roots);
    const content = String(args.content ?? '');
    await fs.mkdir(path.dirname(p), { recursive: true });
    await fs.writeFile(p, content, 'utf-8');
    return { ok: true, bytes: Buffer.byteLength(content, 'utf-8') };
  }
  if (name === 'list_dir') {
    const p = resolveToolPath(String(args.path ?? '.'), roots);
    const entries = await fs.readdir(p);
    return { entries };
  }
  if (name === 'search_files') {
    return await handleSearchFiles(args, roots);
  }
  throw new Error(`unknown local tool: ${String(name)}`);
}

async function handleSearchFiles(args: Record<string, unknown>, roots: string[]): Promise<SearchResult> {
  const query = String(args.query ?? '');
  if (!query) {
    throw new Error('search_files requires query argument (regex pattern)');
  }

  const searchPath = String(args.path ?? '.');
  const cwd = resolveToolPath(searchPath, roots);

  const glob = args.glob ? String(args.glob) : undefined;
  const maxResults = args.max_results ? Number(args.max_results) : 200;

  // Build rg command line arguments
  const rgArgs = [
    '--vimgrep',
    '--no-heading',
    '--color', 'never',
    '--max-columns', '300',
  ];

  if (glob) {
    rgArgs.push('--glob', glob);
  }

  rgArgs.push('--regexp', query, '.');

  try {
    const { stdout } = await execFileAsync(ripgrepBin(), rgArgs, {
      cwd,
      maxBuffer: 8 * 1024 * 1024,
    });

    // Parse vimgrep format: relpath:line:col:text
    const lines = stdout.split('\n').filter((line) => line.length > 0);
    const hits: SearchHit[] = [];

    for (const line of lines) {
      const colonIdx1 = line.indexOf(':');
      if (colonIdx1 === -1) continue;

      const colonIdx2 = line.indexOf(':', colonIdx1 + 1);
      if (colonIdx2 === -1) continue;

      const colonIdx3 = line.indexOf(':', colonIdx2 + 1);
      if (colonIdx3 === -1) continue;

      const file = line.substring(0, colonIdx1);
      const lineNum = Number.parseInt(line.substring(colonIdx1 + 1, colonIdx2), 10);
      // skip column (colonIdx2 + 1 to colonIdx3)
      const text = line.substring(colonIdx3 + 1);

      if (!isNaN(lineNum)) {
        hits.push({ file, line: lineNum, text });
        if (hits.length >= maxResults) {
          break;
        }
      }
    }

    return {
      hits,
      truncated: lines.length > maxResults,
    };
  } catch (err) {
    // rg exit code 1 means "no matches", not an error
    if ((err as NodeJS.ErrnoException).code === 1) {
      return { hits: [], truncated: false };
    }

    // rg missing from PATH
    if ((err as NodeJS.ErrnoException).code === 'ENOENT') {
      throw new Error(
        'ripgrep (rg) not found on PATH — install it (e.g. brew install ripgrep) or set LABMATE_RG_PATH',
      );
    }

    // Other errors (exit code >= 2, etc.)
    const errMsg = (err as any).stderr || String(err);
    throw new Error(`ripgrep failed: ${errMsg}`);
  }
}
