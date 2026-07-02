import { promises as fs, statSync } from 'node:fs';
import path from 'node:path';
import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import { resolveToolPath } from './workspace.js';
import { checkTestCommand } from './permissions.js';

export type LocalToolName = 'read_file' | 'write_file' | 'list_dir' | 'search_files' | 'run_tests';
export const LOCAL_TOOL_NAMES: readonly LocalToolName[] = [
  'read_file',
  'write_file',
  'list_dir',
  'search_files',
  'run_tests',
];

// Exported as a namespace-accessible binding so tests can spy on it
// (handleSearchFiles calls it via this module's namespace).
export const rg = {
  execFileAsync: promisify(execFile),
};

// Exported as a namespace-accessible binding so tests can spy on it
// (handleRunTests calls it via this module's namespace).
export const proc = {
  spawn,
};

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
      if (statSync(p).isFile()) {
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
    const rawOffset = args.offset;
    const rawLimit = args.limit;
    if (rawOffset === undefined && rawLimit === undefined) {
      return { content: await fs.readFile(p, 'utf-8') };
    }
    const text = await fs.readFile(p, 'utf-8');
    const lines = text.split('\n');
    // Restore newlines on all lines except possibly the last (splitlines semantics)
    const linesWithNl = lines.map((l, i) => (i < lines.length - 1 ? l + '\n' : l));
    // Drop the synthetic trailing empty string that split('\n') produces on a
    // newline-terminated file — we don't want it to count as a real line.
    const lastLine = linesWithNl[linesWithNl.length - 1];
    const normalized = lastLine === '' ? linesWithNl.slice(0, -1) : linesWithNl;
    const offset = rawOffset !== undefined ? Math.max(0, Number(rawOffset) - 1) : 0;
    const selected = normalized.slice(offset);
    const limited = rawLimit !== undefined ? selected.slice(0, Number(rawLimit)) : selected;
    return { content: limited.join('') };
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
  if (name === 'run_tests') {
    return await handleRunTests(args, roots);
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
  const rawMax = Number(args.max_results);
  const maxResults = Number.isFinite(rawMax) && rawMax > 0 ? Math.floor(rawMax) : 200;

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
    const { stdout } = await rg.execFileAsync(ripgrepBin(), rgArgs, {
      cwd,
      maxBuffer: 8 * 1024 * 1024,
    });

    // Parse vimgrep format: relpath:line:col:text
    const lines = stdout.split('\n').filter((line) => line.length > 0);
    const hits: SearchHit[] = [];
    let truncated = false;

    for (const line of lines) {
      const colonIdx1 = line.indexOf(':');
      if (colonIdx1 === -1) continue;

      const colonIdx2 = line.indexOf(':', colonIdx1 + 1);
      if (colonIdx2 === -1) continue;

      const colonIdx3 = line.indexOf(':', colonIdx2 + 1);
      if (colonIdx3 === -1) continue;

      const file = line.substring(0, colonIdx1);
      const lineNum = Number.parseInt(line.substring(colonIdx1 + 1, colonIdx2), 10);
      // skip column (colonIdx2 + 1 to colonIdx3); split on the FIRST 3 colons
      // only so any colons inside the matched text are preserved.
      const text = line.substring(colonIdx3 + 1);

      if (!isNaN(lineNum)) {
        if (hits.length >= maxResults) {
          truncated = true;
          break;
        }
        hits.push({ file, line: lineNum, text });
      }
    }

    return { hits, truncated };
  } catch (err) {
    // execFile puts the process EXIT CODE (a number) on `code` for a non-zero
    // exit, or a spawn errno string (e.g. 'ENOENT') when the binary is missing.
    const code = (err as { code?: number | string }).code;
    // rg exit code 1 means "no matches", not an error
    if (code === 1) {
      return { hits: [], truncated: false };
    }

    // rg missing from PATH
    if (code === 'ENOENT') {
      throw new Error(
        'ripgrep (rg) not found on PATH — install it (e.g. brew install ripgrep) or set LABMATE_RG_PATH',
      );
    }

    // Other errors (exit code >= 2, etc.)
    const errMsg = (err as { stderr?: string }).stderr || String(err);
    throw new Error(`ripgrep failed: ${errMsg}`);
  }
}

interface TestResult {
  ok: boolean;
  exit_code: number;
  raw_output: string;
}

/**
 * Run the project's test command in the workspace root.
 * Args: { path?: string (file/dir/pytest nodeid), expr?: string (pytest -k), timeout_ms?: number }
 * Returns { ok, exit_code, raw_output }.
 * Exits with code 126 if the test command is blocked by permissions.
 * Exits with code 124 if the command times out.
 */
async function handleRunTests(args: Record<string, unknown>, roots: string[]): Promise<TestResult> {
  // Workspace root is always roots[0] (the primary root)
  const cwd = resolveToolPath('.', roots);

  // Build the command line from LABMATE_TEST_CMD or default to pytest
  let argv: string[];
  const testCmdEnv = process.env.LABMATE_TEST_CMD;
  if (testCmdEnv) {
    argv = testCmdEnv.split(/\s+/);
  } else {
    argv = ['pytest', '--tb=short', '-q'];
  }

  // Append the path argument if provided
  if (args.path) {
    argv.push(String(args.path));
  }

  // Append -k expr if provided
  if (args.expr) {
    argv.push('-k', String(args.expr));
  }

  // Permission check: ensure the binary is on the allowlist
  if (checkTestCommand(argv) !== 'allow') {
    const bin = String(argv[0] ?? '');
    return {
      ok: false,
      exit_code: 126,
      raw_output: `run_tests blocked: ${bin} is not an allowed test runner`,
    };
  }

  // Parse timeout (default 120s). Guard BOTH the arg and the env var against
  // non-finite/non-positive values — an unguarded NaN (e.g. LABMATE_TEST_TIMEOUT_MS="abc")
  // would feed setTimeout(fn, NaN), firing at ~0ms and killing every run with exit 124.
  const rawTimeoutMs = Number(args.timeout_ms);
  const envTimeoutMs = Number(process.env.LABMATE_TEST_TIMEOUT_MS);
  const timeoutMs =
    Number.isFinite(rawTimeoutMs) && rawTimeoutMs > 0
      ? rawTimeoutMs
      : Number.isFinite(envTimeoutMs) && envTimeoutMs > 0
        ? envTimeoutMs
        : 120000;

  return new Promise<TestResult>((resolve) => {
    let timedOut = false;
    // Buffer ALL output, then head+tail truncate at settle time. pytest failure
    // summaries live at the TAIL, so a head-only cap would drop exactly what the
    // model needs to fix the bug.
    let output = '';
    let killTimeout: ReturnType<typeof setTimeout> | undefined;
    const maxOutputBytes = 200 * 1024; // 200 KB cap (100 KB head + 100 KB tail)
    const halfCap = maxOutputBytes / 2;

    function truncateOutput(raw: string): string {
      if (raw.length <= maxOutputBytes) {
        return raw;
      }
      const dropped = raw.length - maxOutputBytes;
      const head = raw.substring(0, halfCap);
      const tail = raw.substring(raw.length - halfCap);
      return `${head}\n...[truncated ${dropped} bytes]...\n${tail}`;
    }

    const child = proc.spawn(argv[0], argv.slice(1), {
      cwd,
      env: process.env,
    });

    const timeoutHandle = setTimeout(() => {
      timedOut = true;
      if (!child.killed) {
        child.kill('SIGTERM');
        // Grace period for graceful exit, then force-kill. Tracked in outer scope
        // so the 'close'/'error' handlers can clear it — otherwise a dangling timer
        // could fire kill('SIGKILL') after the promise has already settled.
        killTimeout = setTimeout(() => {
          if (!child.killed) {
            child.kill('SIGKILL');
          }
        }, 1000);
      }
    }, timeoutMs);

    // Collect stdout and stderr (full buffer; truncated only at settle time).
    if (child.stdout) {
      child.stdout.on('data', (data: Buffer) => {
        output += data.toString('utf-8');
      });
    }

    if (child.stderr) {
      child.stderr.on('data', (data: Buffer) => {
        output += data.toString('utf-8');
      });
    }

    child.on('close', (code: number | null) => {
      clearTimeout(timeoutHandle);
      if (killTimeout) clearTimeout(killTimeout);

      const rawOutput = truncateOutput(output);

      if (timedOut) {
        const timeoutMsg = `\n[run_tests timed out after ${timeoutMs}ms]`;
        resolve({
          ok: false,
          exit_code: 124,
          raw_output: rawOutput + timeoutMsg,
        });
      } else {
        resolve({
          ok: code === 0,
          exit_code: code ?? -1,
          raw_output: rawOutput,
        });
      }
    });

    child.on('error', (err: Error) => {
      clearTimeout(timeoutHandle);
      if (killTimeout) clearTimeout(killTimeout);
      resolve({
        ok: false,
        exit_code: -1,
        raw_output: `failed to spawn process: ${String(err.message)}`,
      });
    });
  });
}
