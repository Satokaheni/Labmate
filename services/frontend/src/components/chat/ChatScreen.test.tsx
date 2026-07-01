import { describe, it, expect } from 'vitest';
import { scrollSignalFor, findStreamingTurn, isCompactCommand } from './ChatScreen';
import type { Turn } from '@/types/events';

describe('findStreamingTurn', () => {
  it('returns the streaming turn when one exists', () => {
    const turns: Turn[] = [
      { id: 't1', role: 'user', text: 'Hello', status: 'complete' },
      { id: 't2', role: 'assistant', text: 'Hi', status: 'streaming' },
      { id: 't3', role: 'user', text: 'More', status: 'complete' },
    ];
    const streaming = findStreamingTurn(turns);
    expect(streaming?.id).toBe('t2');
  });

  it('returns undefined when no turn is streaming', () => {
    const turns: Turn[] = [
      { id: 't1', role: 'user', text: 'Hello', status: 'complete' },
      { id: 't2', role: 'assistant', text: 'Hi', status: 'complete' },
    ];
    const streaming = findStreamingTurn(turns);
    expect(streaming).toBeUndefined();
  });

  it('returns undefined for empty turns array', () => {
    const turns: Turn[] = [];
    const streaming = findStreamingTurn(turns);
    expect(streaming).toBeUndefined();
  });

  it('returns the first streaming turn if multiple exist (should be rare)', () => {
    const turns: Turn[] = [
      { id: 't1', role: 'assistant', text: 'Hi', status: 'streaming' },
      { id: 't2', role: 'assistant', text: 'More', status: 'streaming' },
    ];
    const streaming = findStreamingTurn(turns);
    expect(streaming?.id).toBe('t1');
  });
});

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

describe('isCompactCommand (the shipped predicate Composer.submit routes on)', () => {
  it('is true for exactly "/compact" (and with surrounding whitespace)', () => {
    expect(isCompactCommand('/compact')).toBe(true);
    expect(isCompactCommand('   /compact  ')).toBe(true);
  });

  it('is false for normal messages, including ones that merely contain /compact', () => {
    expect(isCompactCommand('hello world')).toBe(false);
    expect(isCompactCommand('/compact now')).toBe(false);
    expect(isCompactCommand('please /compact')).toBe(false);
    expect(isCompactCommand('/compactify')).toBe(false);
  });

  it('is false for empty / whitespace-only input', () => {
    expect(isCompactCommand('')).toBe(false);
    expect(isCompactCommand('   ')).toBe(false);
  });
});
