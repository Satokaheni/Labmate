import { promises as fs } from 'node:fs';
import path from 'node:path';

export interface WorkspaceEntry {
  /** Absolute path. */
  absolute: string;
  /** Path to insert into the message (relative to the primary root, or absolute for others). */
  insert: string;
  /** Display label (path relative to its root). */
  display: string;
  /** Root this entry belongs to. */
  root: string;
  isDir: boolean;
}

const SKIP_DIRS = new Set([
  'node_modules', '.git', 'dist', 'dist-electron', '.data', '.codegraph',
  '__pycache__', '.venv', 'venv', '.mypy_cache', '.pytest_cache', 'build', '.next', 'coverage',
]);

const MAX_SCAN = 20000;
const MAX_RESULTS = 30;

/** Case-insensitive subsequence match (fuzzy), like editor file finders. */
function fuzzyMatch(query: string, target: string): boolean {
  if (!query) return true;
  const q = query.toLowerCase();
  const t = target.toLowerCase();
  let qi = 0;
  for (let ti = 0; ti < t.length && qi < q.length; ti++) {
    if (t[ti] === q[qi]) qi++;
  }
  return qi === q.length;
}

/**
 * Search the chat's workspace roots for files/dirs matching `query` (fuzzy on
 * the path relative to each root). `roots[0]` is the primary — entries under it
 * insert as relative paths; entries under other roots insert as absolute paths
 * (so the model's read_file resolves them correctly).
 */
export async function searchWorkspace(roots: string[], query: string): Promise<WorkspaceEntry[]> {
  const results: WorkspaceEntry[] = [];
  let scanned = 0;

  for (let i = 0; i < roots.length && results.length < MAX_RESULTS; i++) {
    const root = path.resolve(roots[i]);
    const isPrimary = i === 0;
    const queue: string[] = [root];

    while (queue.length && scanned < MAX_SCAN && results.length < MAX_RESULTS) {
      const dir = queue.shift()!;
      let dirents;
      try {
        dirents = await fs.readdir(dir, { withFileTypes: true });
      } catch {
        continue;
      }
      for (const d of dirents) {
        if (d.name.startsWith('.') && d.name !== '.env') continue;
        if (d.isDirectory() && SKIP_DIRS.has(d.name)) continue;
        scanned++;
        const abs = path.join(dir, d.name);
        const rel = path.relative(root, abs);
        if (d.isDirectory()) queue.push(abs);
        if (fuzzyMatch(query, rel)) {
          results.push({
            absolute: abs,
            insert: isPrimary ? rel : abs,
            display: rel,
            root,
            isDir: d.isDirectory(),
          });
          if (results.length >= MAX_RESULTS) break;
        }
      }
    }
  }

  // Shorter (shallower) paths first, then alphabetical — most relevant on top.
  results.sort((a, b) => a.display.split('/').length - b.display.split('/').length || a.display.localeCompare(b.display));
  return results.slice(0, MAX_RESULTS);
}
