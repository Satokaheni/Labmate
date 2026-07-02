import { render, screen, cleanup } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { Composer } from './ChatScreen';

const base = {
  mode: 'chat' as const, budget: 1000, sessionId: 's-1',
  onSend: () => {}, onCompact: () => {}, isStreaming: false, onStop: () => {},
};

describe('Composer seed', () => {
  it('is empty with no seed', () => {
    render(<Composer {...base} />);
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('');
  });

  it('prefills the textarea when a seed with a new nonce arrives', () => {
    const { rerender } = render(<Composer {...base} seed={{ text: 'Map the repo', nonce: 1 }} />);
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('Map the repo');
    rerender(<Composer {...base} seed={{ text: 'Explain a diff', nonce: 2 }} />);
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('Explain a diff');
  });

  it('calls onSeedConsumed when a seed is applied', () => {
    const onSeedConsumed = vi.fn();
    render(<Composer {...base} seed={{ text: 'Hello', nonce: 1 }} onSeedConsumed={onSeedConsumed} />);
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('Hello');
    expect(onSeedConsumed).toHaveBeenCalledOnce();
  });

  it('one-shot contract: consumed seed does not re-apply on a fresh mount (simulates send → thread remount)', () => {
    // In real usage: welcome Composer mounts with seed={text, nonce:1}, calls onSeedConsumed,
    // ChatScreen sets seed=null. When turns.length goes 0→1 the Composer remounts in the
    // thread branch. That fresh Composer receives seed=null and must stay empty.
    //
    // We simulate this: mount with seed={text,nonce:1} → onSeedConsumed fires → unmount →
    // mount a FRESH Composer with seed=null (what ChatScreen would now pass).
    const onSeedConsumed = vi.fn();
    const { unmount } = render(
      <Composer {...base} seed={{ text: 'starter text', nonce: 1 }} onSeedConsumed={onSeedConsumed} />
    );
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('starter text');
    expect(onSeedConsumed).toHaveBeenCalledOnce();

    // ChatScreen has now called setSeed(null) — unmount welcome branch and mount thread branch
    unmount();
    cleanup();

    render(<Composer {...base} seed={null} onSeedConsumed={onSeedConsumed} />);
    // Fresh mount with seed=null: textarea must be empty, onSeedConsumed must NOT fire again
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('');
    expect(onSeedConsumed).toHaveBeenCalledOnce(); // still exactly once from the first mount
  });
});
