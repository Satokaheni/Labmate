import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { ThinkingIndicator } from './ThinkingIndicator';
import type { StreamEvent } from '@/types/events';

const ev = (e: StreamEvent): StreamEvent => e;

describe('ThinkingIndicator', () => {
  it('shows Planning on turn.start', () => {
    const events: StreamEvent[] = [ev({ type: 'node.enter', turnId: 't1', node: 'plan_node', thinkingBudget: 2000 })];
    render(<ThinkingIndicator events={events} startedAt={0} now={1000} />);
    expect(screen.getByTestId('phase-label')).toHaveTextContent(/planning/i);
  });

  it('shows Reasoning on reasoning.delta with purple color', () => {
    const events: StreamEvent[] = [
      { type: 'node.enter', turnId: 't1', node: 'plan_node', thinkingBudget: 2000 },
      { type: 'reasoning.delta', turnId: 't1', text: 'thinking' },
    ];
    render(<ThinkingIndicator events={events} startedAt={0} now={1000} />);
    const label = screen.getByTestId('phase-label');
    expect(label).toHaveTextContent(/reasoning/i);
    expect(label).toHaveStyle({ color: '#a78bfa' });
  });

  it('shows the running tool name on tool.start', () => {
    const events: StreamEvent[] = [
      {
        type: 'tool.start',
        turnId: 't1',
        toolCall: { id: 'x', name: 'web_search', kind: 'skill', summary: '', reasoningWhy: 'need facts', args: {} },
      },
    ];
    render(<ThinkingIndicator events={events} startedAt={0} now={500} />);
    expect(screen.getByTestId('phase-label')).toHaveTextContent(/running web_search/i);
  });

  it('renders completed steps as blue checks (no green)', () => {
    const events: StreamEvent[] = [
      { type: 'node.enter', turnId: 't1', node: 'plan_node', thinkingBudget: 2000 },
      {
        type: 'reasoning.done',
        turnId: 't1',
        reasoning: { summary: 'Planned the change', text: '', node: 'plan_node', tokens: 1, budget: 2, durationMs: 1 },
      },
    ];
    const { container } = render(<ThinkingIndicator events={events} startedAt={0} now={1000} />);
    const step = screen.getByTestId('completed-step');
    expect(step).toHaveTextContent('Planned the change');
    expect(step).toHaveTextContent('plan_node');
    // assert no green anywhere in the component
    expect(container.innerHTML).not.toContain('#56c08d');
    expect(container.innerHTML.toLowerCase()).not.toContain('green');
  });

  it('settles to a "Thought for N.Ns" pill on turn.done', () => {
    const events: StreamEvent[] = [
      { type: 'node.enter', turnId: 't1', node: 'plan_node', thinkingBudget: 2000 },
      { type: 'turn.done', turnId: 't1', status: 'complete' },
    ];
    render(<ThinkingIndicator events={events} startedAt={0} now={2400} />);
    expect(screen.getByTestId('thought-pill')).toHaveTextContent(/thought for 2\.4s/i);
    // no active phase label once settled
    expect(screen.queryByTestId('phase-label')).not.toBeInTheDocument();
  });

  it('shows the elapsed timer while active', () => {
    const events: StreamEvent[] = [{ type: 'node.enter', turnId: 't1', node: 'plan_node', thinkingBudget: 2000 }];
    render(<ThinkingIndicator events={events} startedAt={0} now={3200} />);
    expect(screen.getByTestId('elapsed-timer')).toHaveTextContent('3.2s');
  });
});
