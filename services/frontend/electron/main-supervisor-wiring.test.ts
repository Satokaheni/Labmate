import { describe, it, expect, vi } from 'vitest';
import { startupSequence } from './startup-sequence';

describe('startupSequence', () => {
  it('always starts the supervisor with gemmaBase:null (local.env is the source of truth)', async () => {
    const start = vi.fn(async () => {});
    const sup = { start, stop: vi.fn(), onStatus: vi.fn() } as any;
    await startupSequence(sup, 8799, '/repo');
    expect(start).toHaveBeenCalledWith({ gemmaBase: null, localPort: 8799, repoRoot: '/repo', logPath: undefined });
  });

  it('passes logPath through when provided', async () => {
    const start = vi.fn(async () => {});
    const sup = { start, stop: vi.fn(), onStatus: vi.fn() } as any;
    await startupSequence(sup, 8788, '/repo', '/log');
    expect(start).toHaveBeenCalledWith({ gemmaBase: null, localPort: 8788, repoRoot: '/repo', logPath: '/log' });
  });
});
