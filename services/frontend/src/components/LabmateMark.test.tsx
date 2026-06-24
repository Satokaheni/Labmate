import { render } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { LabmateMark } from './LabmateMark';

describe('LabmateMark', () => {
  it('renders a tile variant with a gradient background tile', () => {
    const { container } = render(<LabmateMark size={36} variant="tile" />);
    const tile = container.querySelector('[data-testid="orbit-tile"]') as HTMLElement;
    expect(tile).toBeInTheDocument();
    expect(tile.style.background).toContain('linear-gradient');
    expect(tile.style.width).toBe('36px');
  });

  it('renders onDark variant with a gradient-filled primary body', () => {
    const { container } = render(<LabmateMark size={29} variant="onDark" />);
    // gradient def present, primary body uses url(#...grad)
    expect(container.querySelector('linearGradient')).toBeInTheDocument();
    const primary = container.querySelector('[data-testid="orbit-primary"]') as SVGElement;
    expect(primary.getAttribute('fill')).toMatch(/^url\(#/);
  });

  it('applies fast spin (1.4s) class to the companion group', () => {
    const { container } = render(<LabmateMark size={30} variant="onDark" spin="fast" />);
    expect(container.querySelector('.orbit-spin-fast')).toBeInTheDocument();
  });

  it('applies slow spin (6s) class to the companion group', () => {
    const { container } = render(<LabmateMark size={36} variant="onDark" spin="slow" />);
    expect(container.querySelector('.orbit-spin-slow')).toBeInTheDocument();
  });

  it('applies no spin class when spin is none', () => {
    const { container } = render(<LabmateMark size={18} variant="tile" spin="none" />);
    expect(container.querySelector('.orbit-spin-fast')).not.toBeInTheDocument();
    expect(container.querySelector('.orbit-spin-slow')).not.toBeInTheDocument();
  });

  it('applies the breathe class when breathe is true', () => {
    const { container } = render(<LabmateMark size={36} variant="tile" breathe />);
    expect(container.querySelector('.orbit-breathe')).toBeInTheDocument();
  });
});
