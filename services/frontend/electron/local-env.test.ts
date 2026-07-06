import { describe, it, expect, afterEach } from 'vitest';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { readLocalEnv } from './local-env';

function makeRepoRoot(envContents: string | null): string {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'labmate-local-env-'));
  if (envContents !== null) {
    const infraDir = path.join(tmp, 'infrastructure');
    fs.mkdirSync(infraDir, { recursive: true });
    fs.writeFileSync(path.join(infraDir, 'local.env'), envContents, 'utf8');
  }
  return tmp;
}

describe('readLocalEnv', () => {
  const created: string[] = [];
  afterEach(() => {
    for (const dir of created.splice(0)) {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it('reads LOCAL_PORT + GEMMA_BASE from an explicit-value local.env', async () => {
    const repoRoot = makeRepoRoot(
      'export LOCAL_PORT="8788"\nexport GEMMA_BASE="https://pod-8000.proxy.runpod.net/v1"\n',
    );
    created.push(repoRoot);
    const result = await readLocalEnv(repoRoot);
    expect(result).toEqual({ localPort: 8788, gemmaBase: 'https://pod-8000.proxy.runpod.net/v1' });
  });

  it('falls back to {8787, \'\'} when the file is missing', async () => {
    const repoRoot = makeRepoRoot(null);
    created.push(repoRoot);
    const result = await readLocalEnv(repoRoot);
    expect(result).toEqual({ localPort: 8787, gemmaBase: '' });
  });

  it('resolves a shell-default ${LOCAL_PORT:-8790} form when unset', async () => {
    const repoRoot = makeRepoRoot('export LOCAL_PORT="${LOCAL_PORT:-8790}"\nexport GEMMA_BASE="${GEMMA_BASE:-}"\n');
    created.push(repoRoot);
    const result = await readLocalEnv(repoRoot);
    expect(result).toEqual({ localPort: 8790, gemmaBase: '' });
  });
});
