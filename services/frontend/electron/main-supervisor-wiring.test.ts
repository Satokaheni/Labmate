import { describe, it, expect, vi } from 'vitest';
import { startupSequence } from './startup-sequence';

describe('startupSequence', () => {
  it('starts the supervisor with gemmaBase from config', async () => {
    const start = vi.fn(async () => {});
    const sup = { start, stop: vi.fn(), onStatus: vi.fn() } as any;
    await startupSequence(sup, { wsUrl: 'ws://x', gemmaBase: 'https://m/v1', isDev: false }, 8799, '/repo');
    expect(start).toHaveBeenCalledWith({ gemmaBase: 'https://m/v1', localPort: 8799, repoRoot: '/repo' });
  });

  it('skips the supervisor when no endpoint is configured (onboarding path)', async () => {
    const start = vi.fn(async () => {});
    const sup = { start, stop: vi.fn(), onStatus: vi.fn() } as any;
    await startupSequence(sup, { wsUrl: null, gemmaBase: null, isDev: false }, 8799, '/repo');
    expect(start).not.toHaveBeenCalled();
  });
});
