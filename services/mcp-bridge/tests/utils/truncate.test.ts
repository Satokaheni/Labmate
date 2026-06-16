import { describe, it, expect } from 'vitest';
import { truncate } from '../../src/utils/truncate.js';

describe('truncate', () => {
  it('returns full text when shorter than limit', () => {
    const result = truncate('hello', 0, 100);
    expect(result.text).toBe('hello');
    expect(result.has_more).toBe(false);
    expect(result.next_offset).toBeNull();
    expect(result.total).toBe(5);
  });

  it('truncates at limit and sets has_more', () => {
    const text = 'a'.repeat(30_000);
    const result = truncate(text, 0, 25_000);
    expect(result.text.startsWith('a'.repeat(25_000))).toBe(true);
    expect(result.has_more).toBe(true);
    expect(result.next_offset).toBe(25_000);
    expect(result.total).toBe(30_000);
    expect(result.text).toContain('[TRUNCATED:');
  });

  it('returns second page with correct offset', () => {
    const text = 'a'.repeat(30_000);
    const result = truncate(text, 25_000, 25_000);
    expect(result.text.startsWith('a'.repeat(5_000))).toBe(true);
    expect(result.has_more).toBe(false);
    expect(result.next_offset).toBeNull();
  });

  it('includes next_offset in truncation notice', () => {
    const text = 'x'.repeat(50_000);
    const result = truncate(text, 0, 25_000);
    expect(result.text).toContain('offset=25000');
  });
});
