import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { mkdtempSync, rmSync, writeFileSync, mkdirSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { executeTool } from './tool-executor';

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
