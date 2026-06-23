import { useState, type KeyboardEvent } from 'react';
import type { NodeName } from '@/types/events';

export interface ComposerProps {
  onSend: (text: string) => void;
  onStop: () => void;
  streaming: boolean;
  node?: NodeName;
  thinkingBudget?: number;
  contextPct?: number;
}

export function Composer({ onSend, onStop, streaming, node, thinkingBudget, contextPct }: ComposerProps) {
  const [text, setText] = useState('');

  const send = () => {
    const trimmed = text.trim();
    if (!trimmed) return;
    onSend(trimmed);
    setText('');
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  return (
    <div className="border-t border-border-1 p-3">
      <div className="flex items-end gap-2 rounded-card border border-border-2 bg-panel p-2">
        <textarea
          rows={1}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Message Labmate…"
          className="max-h-40 min-h-[24px] flex-1 resize-none bg-transparent px-2 py-1 text-sm text-primary outline-none placeholder:text-mono"
        />
        {streaming ? (
          <button
            type="button"
            onClick={onStop}
            className="rounded-pill px-3 py-1.5 text-sm font-medium text-page"
            style={{ background: '#ff6b6b' }}
          >
            Stop
          </button>
        ) : (
          <button
            type="button"
            onClick={send}
            disabled={!text.trim()}
            className="rounded-pill px-3 py-1.5 text-sm font-medium text-page disabled:opacity-40"
            style={{ background: 'var(--accent-blue)' }}
          >
            Send
          </button>
        )}
      </div>

      <div data-testid="status-row" className="mt-2 flex items-center gap-3 font-mono text-[11px] text-mono">
        {node && (
          <span className="rounded-pill border border-border-2 px-2 py-0.5" style={{ color: 'var(--accent-blue)' }}>
            {node}
          </span>
        )}
        {thinkingBudget != null && <span>budget {thinkingBudget}</span>}
        {contextPct != null && <span>{contextPct}% context</span>}
      </div>
    </div>
  );
}
