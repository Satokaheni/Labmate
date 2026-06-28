import { renderHook, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { useMinimumElapsed } from './useMinimumElapsed';

describe('useMinimumElapsed', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('stays false while never active', () => {
    const { result } = renderHook(() => useMinimumElapsed(false, 3000));
    expect(result.current).toBe(false);
  });

  it('flips true only after ms, even if active goes false early (fast finish)', () => {
    const { result, rerender } = renderHook(
      ({ a }: { a: boolean }) => useMinimumElapsed(a, 3000),
      { initialProps: { a: false } },
    );
    expect(result.current).toBe(false);

    rerender({ a: true }); // loading started
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    rerender({ a: false }); // finished early at 1s
    expect(result.current).toBe(false); // still held — minimum not reached

    act(() => {
      vi.advanceTimersByTime(2000); // total 3s
    });
    expect(result.current).toBe(true);
  });

  it('stays true once elapsed (does not re-arm)', () => {
    const { result, rerender } = renderHook(
      ({ a }: { a: boolean }) => useMinimumElapsed(a, 1000),
      { initialProps: { a: true } },
    );
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(result.current).toBe(true);

    rerender({ a: false });
    rerender({ a: true });
    expect(result.current).toBe(true);
  });
});
