import { render } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { RegressionPlot, seededScatter } from './RegressionPlot';

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
});
