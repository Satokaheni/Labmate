import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi } from 'vitest';
import { Composer } from './Composer';

describe('Composer', () => {
  it('fires onSend on Enter with the trimmed text', async () => {
    const onSend = vi.fn();
    render(<Composer onSend={onSend} onStop={vi.fn()} streaming={false} />);
    const ta = screen.getByRole('textbox');
    await userEvent.type(ta, 'hello{Enter}');
    expect(onSend).toHaveBeenCalledWith('hello');
  });

  it('does not send on Shift+Enter (adds newline instead)', async () => {
    const onSend = vi.fn();
    render(<Composer onSend={onSend} onStop={vi.fn()} streaming={false} />);
    const ta = screen.getByRole('textbox') as HTMLTextAreaElement;
    await userEvent.type(ta, 'line1{Shift>}{Enter}{/Shift}line2');
    expect(onSend).not.toHaveBeenCalled();
    expect(ta.value).toBe('line1\nline2');
  });

  it('shows a Stop button while streaming and fires onStop', async () => {
    const onStop = vi.fn();
    render(<Composer onSend={vi.fn()} onStop={onStop} streaming />);
    const stop = screen.getByRole('button', { name: /stop/i });
    await userEvent.click(stop);
    expect(onStop).toHaveBeenCalled();
  });

  it('renders the status row with the current node and context percent', () => {
    render(
      <Composer onSend={vi.fn()} onStop={vi.fn()} streaming node="plan_node" thinkingBudget={2000} contextPct={42} />
    );
    expect(screen.getByTestId('status-row')).toHaveTextContent('plan_node');
    expect(screen.getByTestId('status-row')).toHaveTextContent('42%');
  });
});
