import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi } from 'vitest';
import { SessionList } from './SessionList';
import type { Session } from '@/types/events';

const sessions: Session[] = [
  { id: 'a', title: 'Older chat', mode: 'chat', turnCount: 2, contextTokens: 100, createdAt: '2026-06-01T00:00:00Z', updatedAt: '2026-06-01T00:00:00Z' },
  { id: 'b', title: 'Newest chat', mode: 'code', turnCount: 5, contextTokens: 200, createdAt: '2026-06-02T00:00:00Z', updatedAt: '2026-06-03T00:00:00Z' },
];

describe('SessionList', () => {
  it('renders sessions sorted by updatedAt descending', () => {
    render(<SessionList sessions={sessions} activeId={null} onOpen={vi.fn()} />);
    const items = screen.getAllByTestId('session-item');
    expect(items[0]).toHaveTextContent('Newest chat');
    expect(items[1]).toHaveTextContent('Older chat');
  });

  it('fires onOpen with the session id on click', async () => {
    const onOpen = vi.fn();
    render(<SessionList sessions={sessions} activeId={null} onOpen={onOpen} />);
    await userEvent.click(screen.getByText('Older chat'));
    expect(onOpen).toHaveBeenCalledWith('a');
  });

  it('marks the active session', () => {
    render(<SessionList sessions={sessions} activeId="b" onOpen={vi.fn()} />);
    expect(screen.getByText('Newest chat').closest('[data-testid="session-item"]')).toHaveAttribute(
      'data-active',
      'true'
    );
  });

  it('shows a mode icon per session', () => {
    render(<SessionList sessions={sessions} activeId={null} onOpen={vi.fn()} />);
    expect(screen.getByTestId('mode-icon-code')).toBeInTheDocument();
    expect(screen.getByTestId('mode-icon-chat')).toBeInTheDocument();
  });
});
