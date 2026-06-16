import { describe, it, expect } from 'vitest';
import { FsReadInput, FsListInput } from '../../src/schemas/fs.js';
import { GitLogInput, GitStatusInput } from '../../src/schemas/git.js';
import { ExecRunInput } from '../../src/schemas/exec.js';

describe('FsReadInput', () => {
  it('accepts valid input with defaults', () => {
    const result = FsReadInput.safeParse({ path: '/tmp/foo.txt' });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.offset).toBe(0);
      expect(result.data.limit).toBe(25_000);
    }
  });

  it('rejects unknown keys (.strict)', () => {
    const result = FsReadInput.safeParse({ path: '/tmp/foo.txt', unknown: true });
    expect(result.success).toBe(false);
  });

  it('rejects missing required path', () => {
    const result = FsReadInput.safeParse({ offset: 0 });
    expect(result.success).toBe(false);
  });
});

describe('FsListInput', () => {
  it('accepts valid input with defaults', () => {
    const result = FsListInput.safeParse({ path: '/tmp' });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.depth).toBe(2);
    }
  });

  it('rejects unknown keys (.strict)', () => {
    const result = FsListInput.safeParse({ path: '/tmp', unknown: true });
    expect(result.success).toBe(false);
  });

  it('rejects invalid depth', () => {
    const result = FsListInput.safeParse({ path: '/tmp', depth: 0 });
    expect(result.success).toBe(false);
  });
});

describe('GitLogInput', () => {
  it('accepts valid input with defaults', () => {
    const result = GitLogInput.safeParse({ repo_path: '/tmp/repo' });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.max_count).toBe(20);
    }
  });

  it('rejects unknown keys (.strict)', () => {
    const result = GitLogInput.safeParse({ repo_path: '/tmp/repo', unknown: true });
    expect(result.success).toBe(false);
  });

  it('accepts optional branch parameter', () => {
    const result = GitLogInput.safeParse({ repo_path: '/tmp/repo', branch: 'main' });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.branch).toBe('main');
    }
  });
});

describe('GitStatusInput', () => {
  it('accepts valid input', () => {
    const result = GitStatusInput.safeParse({ repo_path: '/tmp/repo' });
    expect(result.success).toBe(true);
  });

  it('rejects unknown keys (.strict)', () => {
    const result = GitStatusInput.safeParse({ repo_path: '/tmp/repo', unknown: true });
    expect(result.success).toBe(false);
  });
});

describe('ExecRunInput', () => {
  it('accepts command and cwd', () => {
    const result = ExecRunInput.safeParse({ command: 'ls', cwd: '/tmp' });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.timeout).toBe(10_000);
    }
  });

  it('rejects missing command', () => {
    const result = ExecRunInput.safeParse({ cwd: '/tmp' });
    expect(result.success).toBe(false);
  });

  it('rejects unknown keys (.strict)', () => {
    const result = ExecRunInput.safeParse({ command: 'ls', cwd: '/tmp', unknown: true });
    expect(result.success).toBe(false);
  });

  it('accepts custom timeout', () => {
    const result = ExecRunInput.safeParse({ command: 'sleep 5', cwd: '/tmp', timeout: 6000 });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.timeout).toBe(6000);
    }
  });
});
