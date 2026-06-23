import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect } from 'vitest';
import { ContextBar } from './ContextBar';
import type { ContextWindow } from '@/types/events';

const win: ContextWindow = {
  max: 8000, used: 4000, free: 4000,
  segments: { systemPrompt: 1000, skillInstructions: 1000, conversation: 1000, workingMemory: 500, reasoning: 500 },
};

describe('ContextBar', () => {
  it('renders used/max', () => {
    render(<ContextBar window={win} />);
    expect(screen.getByTestId('context-usage')).toHaveTextContent('4.0k / 8.0k');
  });

  it('segment widths sum to the used fraction of max (50%)', () => {
    render(<ContextBar window={win} />);
    const segs = screen.getAllByTestId('context-segment');
    const total = segs.reduce((acc, el) => acc + parseFloat((el as HTMLElement).style.width), 0);
    // 5 segments total 4000/8000 = 50%
    expect(Math.round(total)).toBe(50);
  });

  it('expands to show per-segment counts', async () => {
    render(<ContextBar window={win} />);
    expect(screen.queryByText(/systemPrompt/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /context/i }));
    expect(screen.getByText(/systemPrompt/)).toBeInTheDocument();
    expect(screen.getAllByText('1.0k', { exact: false })[0]).toBeInTheDocument();
  });
});
