import { describe, it, expect } from 'vitest';
import { formatDuration, formatTokens, formatBytes } from './format';

describe('formatDuration', () => {
  it('renders seconds with one decimal', () => {
    expect(formatDuration(1500)).toBe('1.5s');
    expect(formatDuration(950)).toBe('0.9s');
  });
});

describe('formatTokens', () => {
  it('renders thousands with k suffix', () => {
    expect(formatTokens(1500)).toBe('1.5k');
    expect(formatTokens(900)).toBe('900');
  });
});

describe('formatBytes', () => {
  it('renders KB/MB', () => {
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(2048)).toBe('2.0 KB');
  });
});
