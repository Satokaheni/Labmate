import { execFile } from 'node:child_process';
import * as path from 'node:path';

export interface LocalEnv { localPort: number; gemmaBase: string; }

/** Read LOCAL_PORT + GEMMA_BASE from infrastructure/local.env by SOURCING it in a
 *  subshell, so shell defaults (${VAR:-default}) resolve exactly as the backend
 *  sees them (the backend sources the same file). Falls back to {8787, ''} if the
 *  file is missing/unreadable. */
export function readLocalEnv(repoRoot: string): Promise<LocalEnv> {
  const envFile = path.join(repoRoot, 'infrastructure', 'local.env');
  return new Promise((resolve) => {
    execFile(
      'bash',
      ['-c', 'source "$1" >/dev/null 2>&1; printf "%s\\n%s" "${LOCAL_PORT:-8787}" "${GEMMA_BASE:-}"', '_', envFile],
      { timeout: 5000 },
      (err, stdout) => {
        if (err) { resolve({ localPort: 8787, gemmaBase: '' }); return; }
        const [port, gemma] = String(stdout).split('\n');
        resolve({ localPort: Number(port) || 8787, gemmaBase: (gemma || '').trim() });
      },
    );
  });
}
