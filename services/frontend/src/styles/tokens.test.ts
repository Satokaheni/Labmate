import { describe, it, expect } from 'vitest';
import css from './tokens.css?raw';

describe('design tokens', () => {
  it('defines core surface tokens', () => {
    expect(css).toContain('--surface-panel: #13161c');
    expect(css).toContain('--surface-rail: #0a0c10');
  });

  it('defines the brand gradient', () => {
    expect(css).toContain('linear-gradient(140deg, #6aa6ff, #a78bfa)');
  });

  it('defines the orbitspin and breathe keyframes', () => {
    expect(css).toContain('@keyframes orbitspin');
    expect(css).toContain('@keyframes breathe');
  });

  it('fast spin is 1.4s and slow spin is 6s', () => {
    expect(css).toContain('animation: orbitspin 1.4s linear infinite');
    expect(css).toContain('animation: orbitspin 6s linear infinite');
  });
});
