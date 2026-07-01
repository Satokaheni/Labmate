import { describe, it, expect } from 'vitest';
import { scrollSignalFor } from './ChatScreen';
import type { Turn } from '@/types/events';

describe('scrollSignalFor', () => {
  it('changes when the last turn gains an artifact', () => {
    const turns1: Turn[] = [
      { id: 't1', role: 'user', text: 'Hello', artifacts: [] },
      { id: 't2', role: 'assistant', text: 'Hi', artifacts: [] },
    ];
    const signal1 = scrollSignalFor(turns1);

    const turns2: Turn[] = [
      { id: 't1', role: 'user', text: 'Hello', artifacts: [] },
      { id: 't2', role: 'assistant', text: 'Hi', artifacts: [
        {
          id: 'a1',
          name: 'file.py',
          path: '/path/file.py',
          language: 'python',
          mime: 'text/x-python',
          sizeBytes: 100,
          preview: 'code',
          content: 'print("hello")',
        },
      ] },
    ];
    const signal2 = scrollSignalFor(turns2);

    expect(signal1).not.toBe(signal2);
  });

  it('changes on new text length', () => {
    const turns1: Turn[] = [
      { id: 't1', role: 'assistant', text: 'Hello' },
    ];
    const signal1 = scrollSignalFor(turns1);

    const turns2: Turn[] = [
      { id: 't1', role: 'assistant', text: 'Hello world' },
    ];
    const signal2 = scrollSignalFor(turns2);

    expect(signal1).not.toBe(signal2);
  });

  it('changes on new toolCalls', () => {
    const turns1: Turn[] = [
      { id: 't1', role: 'assistant', text: 'Running', toolCalls: [] },
    ];
    const signal1 = scrollSignalFor(turns1);

    const turns2: Turn[] = [
      { id: 't1', role: 'assistant', text: 'Running', toolCalls: [
        { id: 'tc1', name: 'test' },
      ] },
    ];
    const signal2 = scrollSignalFor(turns2);

    expect(signal1).not.toBe(signal2);
  });

  it('changes on status change', () => {
    const turns1: Turn[] = [
      { id: 't1', role: 'assistant', text: 'Hi', status: 'streaming' },
    ];
    const signal1 = scrollSignalFor(turns1);

    const turns2: Turn[] = [
      { id: 't1', role: 'assistant', text: 'Hi', status: 'complete' },
    ];
    const signal2 = scrollSignalFor(turns2);

    expect(signal1).not.toBe(signal2);
  });

  it('is stable for empty turns array', () => {
    const turns: Turn[] = [];
    const signal1 = scrollSignalFor(turns);
    const signal2 = scrollSignalFor(turns);

    expect(signal1).toBe(signal2);
  });

  it('handles turns with undefined fields gracefully', () => {
    const turns: Turn[] = [
      { id: 't1', role: 'assistant' },
    ];
    expect(() => {
      scrollSignalFor(turns);
    }).not.toThrow();
  });
});
