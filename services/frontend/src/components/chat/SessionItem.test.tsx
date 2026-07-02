import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { Session } from '@/types/events';
import { SessionItem } from './ChatScreen';

const s: Session = {
  id: 's-1', title: 'My chat', mode: 'chat', turnCount: 3, contextTokens: 0,
  createdAt: '2026-01-01T00:00:00Z', updatedAt: '2026-01-01T00:00:00Z',
};

describe('SessionItem', () => {
  it('keeps its meta line present on hover (no reflow)', () => {
    render(<SessionItem session={s} active={false} onOpen={() => {}} onRename={() => {}} onDelete={() => {}} />);

    // meta line present before hover
    expect(screen.getByText(/3 turns/)).toBeInTheDocument();

    const row = screen.getByRole('button', { name: /My chat/ });
    fireEvent.mouseEnter(row);

    // actions reachable on hover...
    expect(screen.getByTitle('Rename')).toBeInTheDocument();
    expect(screen.getByTitle('Delete')).toBeInTheDocument();
    // ...and the meta line is STILL present (this is what prevents the height change)
    expect(screen.getByText(/3 turns/)).toBeInTheDocument();

    fireEvent.mouseLeave(row);
    expect(screen.getByText(/3 turns/)).toBeInTheDocument();
  });

  it('calls onOpen on Enter keydown', () => {
    const onOpen = vi.fn();
    render(<SessionItem session={s} active={false} onOpen={onOpen} onRename={() => {}} onDelete={() => {}} />);

    const row = screen.getByRole('button', { name: /My chat/ });
    fireEvent.keyDown(row, { key: 'Enter' });

    expect(onOpen).toHaveBeenCalledWith('s-1');
  });

  it('calls onOpen on Space keydown', () => {
    const onOpen = vi.fn();
    render(<SessionItem session={s} active={false} onOpen={onOpen} onRename={() => {}} onDelete={() => {}} />);

    const row = screen.getByRole('button', { name: /My chat/ });
    fireEvent.keyDown(row, { key: ' ' });

    expect(onOpen).toHaveBeenCalledWith('s-1');
  });

  it('does NOT call onOpen when keydown originates from an inner action button', () => {
    const onOpen = vi.fn();
    render(<SessionItem session={s} active={false} onOpen={onOpen} onRename={() => {}} onDelete={() => {}} />);

    const row = screen.getByRole('button', { name: /My chat/ });
    fireEvent.mouseEnter(row);

    const renameBtn = screen.getByTitle('Rename');
    fireEvent.keyDown(renameBtn, { key: 'Enter' });

    expect(onOpen).not.toHaveBeenCalled();
  });
});
