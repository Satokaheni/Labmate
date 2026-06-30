import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { mkdtempSync, rmSync, writeFileSync, mkdirSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { EventEmitter } from 'node:events';
import { executeTool, rg, proc } from './tool-executor';

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

describe('run_tests', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('happy path: fake child emits stdout, exits 0 → {ok:true, exit_code:0}', async () => {
    const fakeChild = new EventEmitter();
    (fakeChild as any).stdout = new EventEmitter();
    (fakeChild as any).stderr = new EventEmitter();
    (fakeChild as any).killed = false;
    (fakeChild as any).kill = vi.fn();

    vi.spyOn(proc, 'spawn').mockReturnValue(fakeChild as never);

    const promise = executeTool('run_tests', { path: 'test_file.py' }, [ws]);

    // Simulate stdout
    (fakeChild as any).stdout.emit('data', Buffer.from('collected 3 items\n'));
    (fakeChild as any).stdout.emit('data', Buffer.from('3 passed\n'));

    // Close with exit code 0
    (fakeChild as any).emit('close', 0);

    const result = (await promise) as { ok: boolean; exit_code: number; raw_output: string };
    expect(result.ok).toBe(true);
    expect(result.exit_code).toBe(0);
    expect(result.raw_output).toContain('3 passed');
  });

  it('failing tests: exit 1 with output → {ok:false, exit_code:1}', async () => {
    const fakeChild = new EventEmitter();
    (fakeChild as any).stdout = new EventEmitter();
    (fakeChild as any).stderr = new EventEmitter();
    (fakeChild as any).killed = false;
    (fakeChild as any).kill = vi.fn();

    vi.spyOn(proc, 'spawn').mockReturnValue(fakeChild as never);

    const promise = executeTool('run_tests', {}, [ws]);

    (fakeChild as any).stdout.emit('data', Buffer.from('FAILED test_x.py::test_fail\n'));
    (fakeChild as any).emit('close', 1);

    const result = (await promise) as { ok: boolean; exit_code: number; raw_output: string };
    expect(result.ok).toBe(false);
    expect(result.exit_code).toBe(1);
    expect(result.raw_output).toContain('FAILED');
  });

  it('blocked command: LABMATE_TEST_CMD="rm -rf /" → {ok:false, exit_code:126, does not spawn}', async () => {
    const spawnSpy = vi.spyOn(proc, 'spawn');

    const originalCmd = process.env.LABMATE_TEST_CMD;
    process.env.LABMATE_TEST_CMD = 'rm -rf /';

    try {
      const result = (await executeTool('run_tests', {}, [ws])) as {
        ok: boolean;
        exit_code: number;
        raw_output: string;
      };

      expect(result.ok).toBe(false);
      expect(result.exit_code).toBe(126);
      expect(result.raw_output).toMatch(/not an allowed test runner/);
      expect(spawnSpy).not.toHaveBeenCalled();
    } finally {
      if (originalCmd !== undefined) {
        process.env.LABMATE_TEST_CMD = originalCmd;
      } else {
        delete process.env.LABMATE_TEST_CMD;
      }
    }
  });

  it('timeout: fake child never closes + timeout_ms: 10 → {ok:false, exit_code:124, kill called}', async () => {
    const fakeChild = new EventEmitter();
    (fakeChild as any).stdout = new EventEmitter();
    (fakeChild as any).stderr = new EventEmitter();
    (fakeChild as any).killed = false;
    const killSpy = vi.fn();
    (fakeChild as any).kill = killSpy;

    vi.spyOn(proc, 'spawn').mockReturnValue(fakeChild as never);

    const promise = executeTool('run_tests', { timeout_ms: 10 }, [ws]);

    // Emit some data but never close
    (fakeChild as any).stdout.emit('data', Buffer.from('running...\n'));

    // Wait for timeout to trigger and then emit close
    await new Promise((resolve) => setTimeout(resolve, 50));
    (fakeChild as any).emit('close', -1);

    const result = (await promise) as { ok: boolean; exit_code: number; raw_output: string };
    expect(result.ok).toBe(false);
    expect(result.exit_code).toBe(124);
    expect(result.raw_output).toMatch(/timed out/);
    expect(killSpy).toHaveBeenCalledWith('SIGTERM');
  });

  it('uses LABMATE_TEST_CMD if set, splitting on whitespace', async () => {
    const fakeChild = new EventEmitter();
    (fakeChild as any).stdout = new EventEmitter();
    (fakeChild as any).stderr = new EventEmitter();
    (fakeChild as any).killed = false;
    (fakeChild as any).kill = vi.fn();

    const spawnSpy = vi.spyOn(proc, 'spawn').mockReturnValue(fakeChild as never);

    const originalCmd = process.env.LABMATE_TEST_CMD;
    process.env.LABMATE_TEST_CMD = 'npm test --coverage';

    try {
      const promise = executeTool('run_tests', {}, [ws]);
      (fakeChild as any).emit('close', 0);
      await promise;

      expect(spawnSpy).toHaveBeenCalledWith('npm', ['test', '--coverage'], expect.any(Object));
    } finally {
      if (originalCmd !== undefined) {
        process.env.LABMATE_TEST_CMD = originalCmd;
      } else {
        delete process.env.LABMATE_TEST_CMD;
      }
    }
  });

  it('appends path argument to command if provided', async () => {
    const fakeChild = new EventEmitter();
    (fakeChild as any).stdout = new EventEmitter();
    (fakeChild as any).stderr = new EventEmitter();
    (fakeChild as any).killed = false;
    (fakeChild as any).kill = vi.fn();

    const spawnSpy = vi.spyOn(proc, 'spawn').mockReturnValue(fakeChild as never);

    const promise = executeTool('run_tests', { path: 'tests/unit' }, [ws]);
    (fakeChild as any).emit('close', 0);
    await promise;

    const call = spawnSpy.mock.calls[0];
    expect(call[1]).toContain('tests/unit');
  });

  it('appends -k expr if expr provided', async () => {
    const fakeChild = new EventEmitter();
    (fakeChild as any).stdout = new EventEmitter();
    (fakeChild as any).stderr = new EventEmitter();
    (fakeChild as any).killed = false;
    (fakeChild as any).kill = vi.fn();

    const spawnSpy = vi.spyOn(proc, 'spawn').mockReturnValue(fakeChild as never);

    const promise = executeTool('run_tests', { expr: 'test_login' }, [ws]);
    (fakeChild as any).emit('close', 0);
    await promise;

    const call = spawnSpy.mock.calls[0];
    const args = call[1];
    expect(args).toContain('-k');
    expect(args).toContain('test_login');
  });

  it('uses env LABMATE_TEST_TIMEOUT_MS for default timeout', async () => {
    const fakeChild = new EventEmitter();
    (fakeChild as any).stdout = new EventEmitter();
    (fakeChild as any).stderr = new EventEmitter();
    (fakeChild as any).killed = false;
    const killSpy = vi.fn();
    (fakeChild as any).kill = killSpy;

    vi.spyOn(proc, 'spawn').mockReturnValue(fakeChild as never);

    const originalEnv = process.env.LABMATE_TEST_TIMEOUT_MS;
    process.env.LABMATE_TEST_TIMEOUT_MS = '50';

    try {
      const promise = executeTool('run_tests', {}, [ws]);

      // Simulate timeout firing and killing the process
      await new Promise((resolve) => setTimeout(resolve, 100));
      (fakeChild as any).emit('close', -1);

      const result = (await promise) as { ok: boolean; exit_code: number; raw_output: string };
      expect(result.ok).toBe(false);
      expect(result.exit_code).toBe(124);
      expect(killSpy).toHaveBeenCalledWith('SIGTERM');
    } finally {
      if (originalEnv !== undefined) {
        process.env.LABMATE_TEST_TIMEOUT_MS = originalEnv;
      } else {
        delete process.env.LABMATE_TEST_TIMEOUT_MS;
      }
    }
  });

  it('collects both stdout and stderr into raw_output', async () => {
    const fakeChild = new EventEmitter();
    (fakeChild as any).stdout = new EventEmitter();
    (fakeChild as any).stderr = new EventEmitter();
    (fakeChild as any).killed = false;
    (fakeChild as any).kill = vi.fn();

    vi.spyOn(proc, 'spawn').mockReturnValue(fakeChild as never);

    const promise = executeTool('run_tests', {}, [ws]);

    (fakeChild as any).stdout.emit('data', Buffer.from('out line 1\n'));
    (fakeChild as any).stderr.emit('data', Buffer.from('err line 1\n'));
    (fakeChild as any).stdout.emit('data', Buffer.from('out line 2\n'));
    (fakeChild as any).emit('close', 0);

    const result = (await promise) as { ok: boolean; raw_output: string };
    expect(result.raw_output).toContain('out line 1');
    expect(result.raw_output).toContain('err line 1');
    expect(result.raw_output).toContain('out line 2');
  });

  it('does NOT timeout when LABMATE_TEST_TIMEOUT_MS is non-numeric (NaN guard)', async () => {
    const fakeChild = new EventEmitter();
    (fakeChild as any).stdout = new EventEmitter();
    (fakeChild as any).stderr = new EventEmitter();
    (fakeChild as any).killed = false;
    const killSpy = vi.fn();
    (fakeChild as any).kill = killSpy;

    vi.spyOn(proc, 'spawn').mockReturnValue(fakeChild as never);

    const originalEnv = process.env.LABMATE_TEST_TIMEOUT_MS;
    process.env.LABMATE_TEST_TIMEOUT_MS = 'abc';

    try {
      const promise = executeTool('run_tests', {}, [ws]);

      // A fast-closing child should resolve normally; a NaN timeout would have
      // fired at ~0ms and forced exit_code 124 with a SIGTERM kill.
      (fakeChild as any).stdout.emit('data', Buffer.from('1 passed\n'));
      (fakeChild as any).emit('close', 0);

      const result = (await promise) as { ok: boolean; exit_code: number; raw_output: string };
      expect(result.ok).toBe(true);
      expect(result.exit_code).toBe(0);
      expect(result.raw_output).toContain('1 passed');
      expect(killSpy).not.toHaveBeenCalled();
    } finally {
      if (originalEnv !== undefined) {
        process.env.LABMATE_TEST_TIMEOUT_MS = originalEnv;
      } else {
        delete process.env.LABMATE_TEST_TIMEOUT_MS;
      }
    }
  });

  it('truncates over-cap output keeping HEAD and TAIL (pytest summary survives)', async () => {
    const fakeChild = new EventEmitter();
    (fakeChild as any).stdout = new EventEmitter();
    (fakeChild as any).stderr = new EventEmitter();
    (fakeChild as any).killed = false;
    (fakeChild as any).kill = vi.fn();

    vi.spyOn(proc, 'spawn').mockReturnValue(fakeChild as never);

    const promise = executeTool('run_tests', {}, [ws]);

    // Emit > 200KB of filler so the cap engages, with a unique marker at the
    // very END (where pytest puts its failure summary).
    const headMarker = 'HEAD_MARKER_START';
    const tailMarker = 'TAIL_FAILURE_SUMMARY_MARKER';
    const filler = 'x'.repeat(300 * 1024); // 300 KB, well over the 200 KB cap
    (fakeChild as any).stdout.emit('data', Buffer.from(headMarker + filler));
    (fakeChild as any).stdout.emit('data', Buffer.from(tailMarker + '\n'));
    (fakeChild as any).emit('close', 1);

    const result = (await promise) as { exit_code: number; raw_output: string };
    expect(result.exit_code).toBe(1);
    // Head preserved
    expect(result.raw_output).toContain(headMarker);
    // Tail preserved — this is the regression guard for the head-only bug
    expect(result.raw_output).toContain(tailMarker);
    // Truncation notice present
    expect(result.raw_output).toMatch(/\[truncated \d+ bytes\]/);
    // And the output is actually shrunk (not the full 300KB+)
    expect(result.raw_output.length).toBeLessThan(300 * 1024);
  });
});
