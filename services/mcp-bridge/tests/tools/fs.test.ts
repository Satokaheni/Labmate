import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('node:fs/promises', () => ({
  readFile: vi.fn(),
  readdir: vi.fn(),
  writeFile: vi.fn(),
}));

import * as fsPromises from 'node:fs/promises';

describe('fs tool handlers', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('makeReadHandler returns file content', async () => {
    vi.mocked(fsPromises.readFile).mockResolvedValue('hello world' as any);
    const { makeReadHandler } = await import('../../src/tools/fs.js');
    const result = await makeReadHandler({ path: '/tmp/foo.txt', offset: 0, limit: 25_000 });
    expect(result.isError).toBeUndefined();
    expect((result.content[0] as any).text).toContain('hello world');
  });

  it('makeReadHandler returns isError:true on ENOENT', async () => {
    vi.mocked(fsPromises.readFile).mockRejectedValue(
      Object.assign(new Error('ENOENT: no such file'), { code: 'ENOENT' })
    );
    const { makeReadHandler } = await import('../../src/tools/fs.js');
    const result = await makeReadHandler({ path: '/nonexistent.txt', offset: 0, limit: 25_000 });
    expect(result.isError).toBe(true);
    expect((result.content[0] as any).text).toContain('ENOENT');
  });

  it('makeWriteHandler returns isError:true on permission denied', async () => {
    vi.mocked(fsPromises.writeFile).mockRejectedValue(
      Object.assign(new Error('EACCES: permission denied'), { code: 'EACCES' })
    );
    const { makeWriteHandler } = await import('../../src/tools/fs.js');
    const result = await makeWriteHandler({ path: '/root/secret', content: 'x' });
    expect(result.isError).toBe(true);
    expect((result.content[0] as any).text).toContain('EACCES');
  });

  it('makeListHandler returns directory entries', async () => {
    vi.mocked(fsPromises.readdir).mockResolvedValue([
      { name: 'file.txt', isDirectory: () => false } as any,
      { name: 'subdir', isDirectory: () => true } as any,
    ]);
    const { makeListHandler } = await import('../../src/tools/fs.js');
    const result = await makeListHandler({ path: '/tmp', depth: 1 });
    expect(result.isError).toBeUndefined();
    const text = (result.content[0] as any).text;
    expect(text).toContain('file.txt');
    expect(text).toContain('subdir');
  });
});
