import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect } from 'vitest';
import { ToolCallRow } from './ToolCallRow';
import type { ToolCall } from '@/types/events';

const tc: ToolCall = {
  id: 't1', name: 'web_search', kind: 'skill', status: 'done',
  summary: 'found 3 results', durationMs: 1200,
  reasoningWhy: 'needed current facts', args: { q: 'labmate' }, result: { hits: 3 },
};

describe('ToolCallRow', () => {
  it('collapsed: shows name, summary and duration', () => {
    render(<ToolCallRow toolCall={tc} />);
    expect(screen.getByText('web_search')).toBeInTheDocument();
    expect(screen.getByText(/found 3 results/)).toBeInTheDocument();
    expect(screen.getByText('1.2s')).toBeInTheDocument();
  });

  it('collapsed: does not show reasoningWhy', () => {
    render(<ToolCallRow toolCall={tc} />);
    expect(screen.queryByText(/needed current facts/)).not.toBeInTheDocument();
  });

  it('expands to show reasoningWhy, args and result', async () => {
    render(<ToolCallRow toolCall={tc} />);
    await userEvent.click(screen.getByTestId('tool-call-row'));
    expect(screen.getByText(/needed current facts/)).toBeInTheDocument();
    expect(screen.getByText(/"q": "labmate"/)).toBeInTheDocument();
    expect(screen.getByText(/"hits": 3/)).toBeInTheDocument();
  });

  it('shows a running spinner when status is running', () => {
    render(<ToolCallRow toolCall={{ ...tc, status: 'running' }} />);
    expect(screen.getByTestId('tool-spinner')).toBeInTheDocument();
  });
});
