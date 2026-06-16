import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('node:child_process');
vi.mock('node:util');

describe('git tool handlers', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('makeStatusHandler returns git status output', async () => {
    vi.clearAllMocks();
    const { promisify } = await import('node:util');

    const mockExecFileAsync = vi.fn(async () => ({
      stdout: '## main\nM file.txt\n',
      stderr: '',
    }));
    vi.mocked(promisify).mockReturnValue(mockExecFileAsync as any);

    // Re-import after mocking to get fresh instance
    vi.resetModules();
    const { makeStatusHandler } = await import('../../src/tools/git.js');
    const result = await makeStatusHandler({ repo_path: '/tmp/repo' });
    expect(result.isError).toBeUndefined();
    expect((result.content[0] as any).text).toContain('main');
  });

  it('makeStatusHandler returns isError:true when not a git repo', async () => {
    vi.clearAllMocks();
    const { promisify } = await import('node:util');

    const mockExecFileAsync = vi.fn(async () => {
      throw new Error('not a git repository');
    });
    vi.mocked(promisify).mockReturnValue(mockExecFileAsync as any);

    vi.resetModules();
    const { makeStatusHandler } = await import('../../src/tools/git.js');
    const result = await makeStatusHandler({ repo_path: '/tmp/notgit' });
    expect(result.isError).toBe(true);
    expect((result.content[0] as any).text).toContain('not a git repository');
  });

  it('makeLogHandler returns formatted commit log', async () => {
    vi.clearAllMocks();
    const { promisify } = await import('node:util');

    const mockExecFileAsync = vi.fn(async () => ({
      stdout: 'abc1234 Fix bug\ndef5678 Add feature\n',
      stderr: '',
    }));
    vi.mocked(promisify).mockReturnValue(mockExecFileAsync as any);

    vi.resetModules();
    const { makeLogHandler } = await import('../../src/tools/git.js');
    const result = await makeLogHandler({ repo_path: '/tmp/repo', max_count: 20 });
    expect(result.isError).toBeUndefined();
    expect((result.content[0] as any).text).toContain('abc1234');
  });

  it('makeDiffHandler returns diff output', async () => {
    vi.clearAllMocks();
    const { promisify } = await import('node:util');

    const mockExecFileAsync = vi.fn(async () => ({
      stdout: '+++ b/file.txt\n-old line\n+new line\n',
      stderr: '',
    }));
    vi.mocked(promisify).mockReturnValue(mockExecFileAsync as any);

    vi.resetModules();
    const { makeDiffHandler } = await import('../../src/tools/git.js');
    const result = await makeDiffHandler({ repo_path: '/tmp/repo', ref_a: 'HEAD' });
    expect(result.isError).toBeUndefined();
    expect((result.content[0] as any).text).toContain('+new line');
  });
});
