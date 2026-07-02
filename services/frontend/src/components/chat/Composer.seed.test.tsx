import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
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
});
