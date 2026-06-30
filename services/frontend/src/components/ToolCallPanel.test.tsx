import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { ToolCallPanel } from './ToolCallPanel';
import type { Turn as TurnT } from '@/types/events';

const makeTurn = (id: string, toolCalls: TurnT['toolCalls']): TurnT => ({
  id,
  sessionId: 's1',
  role: 'assistant',
  text: '',
  createdAt: new Date().toISOString(),
  status: 'complete',
  toolCalls,
});

describe('ToolCallPanel', () => {
  it('shows empty state when no assistant turns have tool calls', () => {
    const turns: TurnT[] = [
      { id: 'u1', sessionId: 's1', role: 'user', text: 'hi', createdAt: new Date().toISOString() },
      makeTurn('a1', []),
    ];
    render(<ToolCallPanel turns={turns} />);
    expect(screen.getByText('No tool calls yet')).toBeInTheDocument();
  });

  it('renders tool call rows for assistant turns with tool calls', () => {
    const turns: TurnT[] = [
      makeTurn('a1', [
        {
          id: 'tc1',
          name: 'read_file',
          kind: 'tool',
          status: 'done',
          summary: 'read it',
          durationMs: 100,
          reasoningWhy: 'needed',
          args: {},
          result: {},
        },
      ]),
    ];
    render(<ToolCallPanel turns={turns} />);
    expect(screen.getByTestId('tool-call-row')).toBeInTheDocument();
    expect(screen.getByText('read_file')).toBeInTheDocument();
  });

  it('shows turn id slices as group headers', () => {
    const turns: TurnT[] = [
      makeTurn('turn-abc123', [
        {
          id: 'tc1',
          name: 'search',
          kind: 'tool',
          status: 'done',
          summary: 's',
          durationMs: 10,
          reasoningWhy: '',
          args: {},
          result: {},
        },
      ]),
    ];
    render(<ToolCallPanel turns={turns} />);
    expect(screen.getByText('turn abc123')).toBeInTheDocument();
  });
});
