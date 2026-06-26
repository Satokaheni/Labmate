import { render, screen, fireEvent } from '@testing-library/react';
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

  it('shows the delete button on hover and calls onDelete when clicked', async () => {
    const onDelete = vi.fn();
    const { container } = render(
      <SessionList sessions={sessions} activeId={null} onOpen={vi.fn()} onDelete={onDelete} />
    );

    const listItem = container.querySelector('li');
    expect(listItem).not.toBeNull();
    expect(screen.queryByTestId('session-delete')).not.toBeInTheDocument();

    fireEvent.mouseEnter(listItem!);

    const deleteButton = screen.getByTestId('session-delete');
    expect(deleteButton).toBeInTheDocument();

    await userEvent.click(deleteButton);
    expect(onDelete).toHaveBeenCalledWith(sessions[1].id);
  });

  it('does not show the delete button when onDelete is not provided', () => {
    const { container } = render(
      <SessionList sessions={sessions} activeId={null} onOpen={vi.fn()} />
    );

    const listItem = container.querySelector('li');
    fireEvent.mouseEnter(listItem!);

    expect(screen.queryByTestId('session-delete')).not.toBeInTheDocument();
  });

  it('shows rename button on hover and activates inline edit with existing title', () => {
    const testSessions: Session[] = [
      { id: 's1', title: 'My chat', mode: 'chat' as const, turnCount: 2,
        contextTokens: 0, updatedAt: new Date().toISOString(), createdAt: new Date().toISOString() },
    ];
    const onRename = vi.fn();
    const { container } = render(<SessionList sessions={testSessions} activeId={null} onOpen={() => {}} onRename={onRename} />);

    const listItem = container.querySelector('li');
    fireEvent.mouseEnter(listItem!);
    const renameBtn = screen.getByTestId('session-rename');
    expect(renameBtn).toBeInTheDocument();

    fireEvent.click(renameBtn);
    const input = screen.getByTestId('session-rename-input');
    expect(input).toHaveValue('My chat');

    fireEvent.change(input, { target: { value: 'New title' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onRename).toHaveBeenCalledWith('s1', 'New title');
  });

  it('cancels rename on Escape without calling onRename', () => {
    const testSessions: Session[] = [
      { id: 's1', title: 'My chat', mode: 'chat' as const, turnCount: 2,
        contextTokens: 0, updatedAt: new Date().toISOString(), createdAt: new Date().toISOString() },
    ];
    const onRename = vi.fn();
    const { container } = render(<SessionList sessions={testSessions} activeId={null} onOpen={() => {}} onRename={onRename} />);

    const listItem = container.querySelector('li');
    fireEvent.mouseEnter(listItem!);
    fireEvent.click(screen.getByTestId('session-rename'));
    fireEvent.keyDown(screen.getByTestId('session-rename-input'), { key: 'Escape' });
    expect(onRename).not.toHaveBeenCalled();
    expect(screen.queryByTestId('session-rename-input')).not.toBeInTheDocument();
  });

  it('does not show rename button when onRename is not provided', () => {
    const { container } = render(
      <SessionList sessions={sessions} activeId={null} onOpen={() => {}} />
    );
    const listItem = container.querySelector('li');
    fireEvent.mouseEnter(listItem!);
    expect(screen.queryByTestId('session-rename')).not.toBeInTheDocument();
  });

  it('commits rename on input blur', () => {
    const testSessions: Session[] = [
      { id: 's1', title: 'Old title', mode: 'chat' as const, turnCount: 0,
        contextTokens: 0, updatedAt: new Date().toISOString(), createdAt: new Date().toISOString() },
    ];
    const onRename = vi.fn();
    render(<SessionList sessions={testSessions} activeId={null} onOpen={() => {}} onRename={onRename} />);

    fireEvent.mouseEnter(screen.getByRole('listitem'));
    fireEvent.click(screen.getByTestId('session-rename'));
    const input = screen.getByTestId('session-rename-input');
    fireEvent.change(input, { target: { value: 'Blurred title' } });
    fireEvent.blur(input);
    expect(onRename).toHaveBeenCalledWith('s1', 'Blurred title');
  });

  it('does not call onRename when committed value is empty whitespace', () => {
    const testSessions: Session[] = [
      { id: 's1', title: 'My chat', mode: 'chat' as const, turnCount: 0,
        contextTokens: 0, updatedAt: new Date().toISOString(), createdAt: new Date().toISOString() },
    ];
    const onRename = vi.fn();
    render(<SessionList sessions={testSessions} activeId={null} onOpen={() => {}} onRename={onRename} />);

    fireEvent.mouseEnter(screen.getByRole('listitem'));
    fireEvent.click(screen.getByTestId('session-rename'));
    fireEvent.change(screen.getByTestId('session-rename-input'), { target: { value: '   ' } });
    fireEvent.keyDown(screen.getByTestId('session-rename-input'), { key: 'Enter' });
    expect(onRename).not.toHaveBeenCalled();
  });
});
