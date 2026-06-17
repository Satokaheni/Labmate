import { describe, it, expect } from 'vitest';
import { formatError } from '../../src/utils/formatError.js';

describe('formatError', () => {
  it('includes message, code, context, and stack', () => {
    const err = Object.assign(new Error('ENOENT: no such file'), { code: 'ENOENT' });
    const out = formatError(err, { path: '/tmp/missing.txt' });
    expect(out).toContain('message: ENOENT: no such file');
    expect(out).toContain('code: ENOENT');
    expect(out).toContain('context: {"path":"/tmp/missing.txt"}');
    expect(out).toContain('stack:');
  });

  it('handles non-Error objects', () => {
    const out = formatError('plain string error', { tool: 'fs_read_file' });
    expect(out).toContain('message: plain string error');
    expect(out).toContain('context:');
  });

  it('omits code line when code is not present', () => {
    const err = new Error('generic failure');
    const out = formatError(err, { cmd: 'ls' });
    expect(out).not.toContain('code:');
    expect(out).toContain('message: generic failure');
  });
});
