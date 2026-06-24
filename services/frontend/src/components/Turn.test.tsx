import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect } from 'vitest';
import { Turn } from './Turn';
import type { Turn as TurnT } from '@/types/events';

const userTurn: TurnT = {
  id: 'u1', sessionId: 's1', role: 'user', text: 'hello agent', createdAt: '2026-06-23T00:00:00Z',
};

const assistantTurn: TurnT = {
  id: 'a1', sessionId: 's1', role: 'assistant', text: 'Here is the **answer**.', createdAt: '2026-06-23T00:00:01Z',
  status: 'complete',
  reasoning: {
    summary: 'Plan the change', text: 'Long reasoning trace here.', node: 'plan_node',
    tokens: 120, budget: 2000, durationMs: 1500,
  },
};

describe('Turn', () => {
  it('renders user text right-aligned with no avatar', () => {
    render(<Turn turn={userTurn} />);
    expect(screen.getByText('hello agent')).toBeInTheDocument();
    expect(screen.queryByTestId('orbit-mark')).not.toBeInTheDocument();
    expect(screen.getByTestId('user-turn')).toHaveClass('justify-end');
  });

  it('renders assistant turn with a LabmateMark avatar', () => {
    render(<Turn turn={assistantTurn} />);
    expect(screen.getByTestId('orbit-mark')).toBeInTheDocument();
  });

  it('renders the assistant answer markdown', () => {
    const { container } = render(<Turn turn={assistantTurn} />);
    expect(container.querySelector('strong')).toHaveTextContent('answer');
  });

  it('collapses the reasoning block by default and expands on click', async () => {
    render(<Turn turn={assistantTurn} />);
    expect(screen.queryByText('Long reasoning trace here.')).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /expand reasoning/i }));
    expect(screen.getByText('Long reasoning trace here.')).toBeInTheDocument();
  });
});
