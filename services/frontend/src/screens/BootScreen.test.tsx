import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi } from 'vitest';
import { BootScreen } from './BootScreen';
import type { Subsystem } from '@/types/events';

const plan: Subsystem[] = [
  { id: 'brain', label: 'Brain', detail: 'llama.cpp :8000', state: 'pending', required: true },
  { id: 'nervous_system', label: 'Nervous System', detail: 'MCP bridge', state: 'pending', required: true },
  { id: 'hands', label: 'Hands', detail: 'skills', state: 'pending', required: false },
];

describe('BootScreen', () => {
  it('renders zero subsystem rows before a boot.plan arrives', () => {
    render(<BootScreen subsystems={[]} onRetry={vi.fn()} />);
    expect(screen.queryAllByTestId('subsystem-row')).toHaveLength(0);
  });

  it('renders one row per subsystem after boot.plan', () => {
    render(<BootScreen subsystems={plan} onRetry={vi.fn()} />);
    expect(screen.getAllByTestId('subsystem-row')).toHaveLength(3);
  });

  it('computes progress as ready/total', () => {
    const subs = plan.map((s, i) => (i === 0 ? { ...s, state: 'ready' as const } : s));
    render(<BootScreen subsystems={subs} onRetry={vi.fn()} />);
    expect(screen.getByTestId('boot-progress')).toHaveAttribute('aria-valuenow', '33');
  });

  it('shows a spinner for a starting subsystem', () => {
    const subs = [{ ...plan[0], state: 'starting' as const }, ...plan.slice(1)];
    render(<BootScreen subsystems={subs} onRetry={vi.fn()} />);
    expect(screen.getByTestId('row-spinner-brain')).toBeInTheDocument();
  });

  it('shows a check for a ready subsystem', () => {
    const subs = [{ ...plan[0], state: 'ready' as const }, ...plan.slice(1)];
    render(<BootScreen subsystems={subs} onRetry={vi.fn()} />);
    expect(screen.getByTestId('row-check-brain')).toBeInTheDocument();
  });

  it('shows a Retry button for a required failed subsystem and fires onRetry', async () => {
    const onRetry = vi.fn();
    const subs = [{ ...plan[0], state: 'failed' as const, message: 'no /healthz' }, ...plan.slice(1)];
    render(<BootScreen subsystems={subs} onRetry={onRetry} />);
    const btn = screen.getByRole('button', { name: /retry/i });
    await userEvent.click(btn);
    expect(onRetry).toHaveBeenCalledWith('brain');
  });

  it('does not show Retry for an optional failed subsystem', () => {
    const subs = [...plan.slice(0, 2), { ...plan[2], state: 'failed' as const }];
    render(<BootScreen subsystems={subs} onRetry={vi.fn()} />);
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
  });
});
