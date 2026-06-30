import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { mkdtempSync, rmSync, writeFileSync, mkdirSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { executeTool, rg } from './tool-executor';

let ws: string;

beforeEach(() => {
  ws = mkdtempSync(path.join(tmpdir(), 'labmate-'));
});
afterEach(() => {
  rmSync(ws, { recursive: true, force: true });
});

describe('executeTool', () => {
  it('read_file returns content (relative resolves against primary root)', async () => {
    writeFileSync(path.join(ws, 'a.txt'), 'hello', 'utf-8');
    const out = await executeTool('read_file', { path: 'a.txt' }, [ws]);
    expect(out).toEqual({ content: 'hello' });
  });

  it('read_file accepts an absolute path inside a secondary root', async () => {
    const ws2 = mkdtempSync(path.join(tmpdir(), 'labmate2-'));
    try {
      writeFileSync(path.join(ws2, 'b.txt'), 'world', 'utf-8');
      const out = await executeTool('read_file', { path: path.join(ws2, 'b.txt') }, [ws, ws2]);
      expect(out).toEqual({ content: 'world' });
    } finally {
      rmSync(ws2, { recursive: true, force: true });
    }
  });

  it('write_file creates nested file', async () => {
    const out = await executeTool('write_file', { path: 'sub/b.txt', content: 'data' }, [ws]);
    expect((out as { ok: boolean }).ok).toBe(true);
    expect(readFileSync(path.join(ws, 'sub', 'b.txt'), 'utf-8')).toBe('data');
  });

  it('list_dir lists entries', async () => {
    writeFileSync(path.join(ws, 'x.txt'), '1', 'utf-8');
    mkdirSync(path.join(ws, 'd'));
    const out = (await executeTool('list_dir', { path: '.' }, [ws])) as { entries: string[] };
    expect(out.entries.sort()).toEqual(['d', 'x.txt']);
  });

  it('rejects relative path escape', async () => {
    await expect(executeTool('read_file', { path: '../../etc/passwd' }, [ws])).rejects.toThrow(
      /outside the primary/,
    );
  });

  it('rejects absolute path outside all roots', async () => {
    await expect(executeTool('read_file', { path: '/etc/passwd' }, [ws])).rejects.toThrow(
      /outside all workspace roots/,
    );
  });

  it('throws when no roots are configured', async () => {
    await expect(executeTool('read_file', { path: 'a.txt' }, [])).rejects.toThrow(/no workspace set/);
  });

  it('rejects unknown tool', async () => {
    await expect(executeTool('rm_rf' as never, {}, [ws])).rejects.toThrow(/unknown local tool/);
  });
});

describe('search_files', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('parses multi-line vimgrep stdout, preserving colons in matched text', async () => {
    const stdout =
      './a.ts:12:5:const x = 1\n./b.ts:3:1:url: https://a.com:8080/x\n';
    vi.spyOn(rg, 'execFileAsync').mockResolvedValue({ stdout, stderr: '' } as never);

    const result = (await executeTool('search_files', { query: 'x' }, [ws])) as {
      hits: Array<{ file: string; line: number; text: string }>;
      truncated: boolean;
    };

    expect(result.hits).toEqual([
      { file: './a.ts', line: 12, text: 'const x = 1' },
      { file: './b.ts', line: 3, text: 'url: https://a.com:8080/x' },
    ]);
    expect(result.truncated).toBe(false);
  });

  it('returns empty hits on rg exit code 1 (no matches), without throwing', async () => {
    const err = Object.assign(new Error('exit 1'), { code: 1 });
    vi.spyOn(rg, 'execFileAsync').mockRejectedValue(err);

    const result = (await executeTool('search_files', { query: 'nope' }, [ws])) as {
      hits: unknown[];
      truncated: boolean;
    };

    expect(result.hits).toEqual([]);
    expect(result.truncated).toBe(false);
  });

  it('caps hits to max_results and reports truncated', async () => {
    const stdout = Array.from({ length: 5 }, (_, i) => `./f${i}.ts:${i + 1}:1:match ${i}`).join(
      '\n',
    );
    vi.spyOn(rg, 'execFileAsync').mockResolvedValue({ stdout, stderr: '' } as never);

    const result = (await executeTool(
      'search_files',
      { query: 'match', max_results: 3 },
      [ws],
    )) as { hits: unknown[]; truncated: boolean };

    expect(result.hits.length).toBe(3);
    expect(result.truncated).toBe(true);
  });

  it('throws with rg stderr on exit code >= 2', async () => {
    const err = Object.assign(new Error('exit 2'), {
      code: 2,
      stderr: 'regex parse error',
    });
    vi.spyOn(rg, 'execFileAsync').mockRejectedValue(err);

    await expect(executeTool('search_files', { query: '(' }, [ws])).rejects.toThrow(
      /regex parse error/,
    );
  });

  it('throws when passed outside the workspace roots', async () => {
    await expect(
      executeTool('search_files', { query: 'test', path: '../../etc/passwd' }, [ws]),
    ).rejects.toThrow(/outside the primary/);
  });

  it('throws when no query is provided', async () => {
    await expect(executeTool('search_files', { query: '' }, [ws])).rejects.toThrow(
      /search_files requires query argument/,
    );
  });

  it('throws when ripgrep is not on PATH', async () => {
    // Set an invalid rg path to trigger the ENOENT error
    const originalPath = process.env.LABMATE_RG_PATH;
    process.env.LABMATE_RG_PATH = '/nonexistent/path/to/rg_that_does_not_exist';

    try {
      writeFileSync(path.join(ws, 'test.txt'), 'content', 'utf-8');
      await expect(
        executeTool('search_files', { query: 'test' }, [ws]),
      ).rejects.toThrow(/ripgrep \(rg\) not found on PATH/);
    } finally {
      if (originalPath !== undefined) {
        process.env.LABMATE_RG_PATH = originalPath;
      } else {
        delete process.env.LABMATE_RG_PATH;
      }
    }
  });
});
