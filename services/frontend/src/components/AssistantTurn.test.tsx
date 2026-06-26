import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { AssistantTurn } from './AssistantTurn';
import type { Turn as TurnT } from '@/types/events';

describe('AssistantTurn', () => {
  it('shows inline tool calls by default', () => {
    const turn: TurnT = {
      id: 't1',
      sessionId: 's1',
      role: 'assistant',
      text: 'done',
      createdAt: new Date().toISOString(),
      status: 'complete',
      toolCalls: [
        {
          id: 'tc1',
          name: 'read_file',
          kind: 'tool',
          status: 'done',
          summary: 'read foo.ts',
          durationMs: 100,
          reasoningWhy: 'needed',
          args: {},
          result: {},
        },
      ],
    };
    render(<AssistantTurn turn={turn} />);
    expect(screen.getByTestId('tool-call-row')).toBeInTheDocument();
    expect(screen.queryByTestId('tool-calls-summary')).not.toBeInTheDocument();
  });

  it('shows summary pill when hideToolCalls is true', () => {
    const turn: TurnT = {
      id: 't1',
      sessionId: 's1',
      role: 'assistant',
      text: 'done',
      createdAt: new Date().toISOString(),
      status: 'complete',
      toolCalls: [
        {
          id: 'tc1',
          name: 'read_file',
          kind: 'tool',
          status: 'done',
          summary: 'read foo.ts',
          durationMs: 100,
          reasoningWhy: 'needed',
          args: {},
          result: {},
        },
        {
          id: 'tc2',
          name: 'write_file',
          kind: 'tool',
          status: 'done',
          summary: 'wrote bar.ts',
          durationMs: 200,
          reasoningWhy: 'needed',
          args: {},
          result: {},
        },
      ],
    };
    render(<AssistantTurn turn={turn} hideToolCalls />);
    expect(screen.queryByTestId('tool-call-row')).not.toBeInTheDocument();
    expect(screen.getByTestId('tool-calls-summary')).toHaveTextContent('2 tool calls → panel');
  });

  it('shows singular form for one tool call summary', () => {
    const turn: TurnT = {
      id: 't1',
      sessionId: 's1',
      role: 'assistant',
      text: '',
      createdAt: new Date().toISOString(),
      status: 'complete',
      toolCalls: [
        {
          id: 'tc1',
          name: 'search',
          kind: 'tool',
          status: 'done',
          summary: 'searched',
          durationMs: 50,
          reasoningWhy: 'needed',
          args: {},
          result: {},
        },
      ],
    };
    render(<AssistantTurn turn={turn} hideToolCalls />);
    expect(screen.getByTestId('tool-calls-summary')).toHaveTextContent('1 tool call → panel');
  });
});
