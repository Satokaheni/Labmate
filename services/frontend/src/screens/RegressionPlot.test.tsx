import { render } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import { RegressionPlot } from './RegressionPlot';
import { seededScatter } from '@/lib/scatter';

describe('seededScatter', () => {
  it('is deterministic for seed 1337', () => {
    const a = seededScatter(1337, 24);
    const b = seededScatter(1337, 24);
    expect(a).toEqual(b);
    expect(a).toHaveLength(24);
  });

  it('produces different output for a different seed', () => {
    const a = seededScatter(1337, 24);
    const c = seededScatter(42, 24);
    expect(a).not.toEqual(c);
  });
});

describe('RegressionPlot', () => {
  it('renders an svg with the requested number of points', () => {
    const { container } = render(<RegressionPlot progress={0.5} seed={1337} count={24} />);
    expect(container.querySelector('svg')).toBeInTheDocument();
    expect(container.querySelectorAll('[data-testid="scatter-point"]')).toHaveLength(24);
  });

  it('sets up the RAF animation effect once on mount and does not restart on progress changes', () => {
    const rafSpy = vi.spyOn(window, 'requestAnimationFrame');
    const cancelSpy = vi.spyOn(window, 'cancelAnimationFrame');

    const { rerender } = render(<RegressionPlot progress={0} seed={1337} count={24} />);

    // After initial render, RAF should have been called at least once to start the loop
    const rafCallsAfterMount = rafSpy.mock.calls.length;
    expect(rafCallsAfterMount).toBeGreaterThanOrEqual(1);

    // Record the number of cancelAnimationFrame calls after mount (should be 0 since effect just set up)
    const cancelCallsBeforeRerender = cancelSpy.mock.calls.length;

    // Rerender with a different progress value
    rerender(<RegressionPlot progress={0.5} seed={1337} count={24} />);

    // The effect should NOT have rerun (no cleanup + restart)
    // This means cancelAnimationFrame should NOT have been called due to effect cleanup
    expect(cancelSpy.mock.calls.length).toBe(cancelCallsBeforeRerender);

    // RAF should not have been called again (the effect dependency array is now empty)
    expect(rafSpy.mock.calls.length).toBe(rafCallsAfterMount);

    rafSpy.mockRestore();
    cancelSpy.mockRestore();
  });

  it('renders the regression plot svg element with scatter points', () => {
    const { getByTestId, getAllByTestId } = render(<RegressionPlot progress={0.3} seed={1337} count={24} />);

    const svg = getByTestId('regression-plot');
    expect(svg).toBeInTheDocument();
    expect(svg.tagName).toBe('svg');

    const points = getAllByTestId('scatter-point');
    expect(points).toHaveLength(24);
    points.forEach((point) => {
      expect(point.tagName).toBe('circle');
    });
  });
});
