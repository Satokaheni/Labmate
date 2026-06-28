import { useState } from 'react';
import type { Reasoning } from '@/types/events';
import { formatTokens, formatDuration } from '@/lib/format';

export function ReasoningBlock({ reasoning }: { reasoning: Reasoning }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mb-2 rounded-pill border border-border-2 bg-page/40">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-label={open ? 'Collapse reasoning' : 'Expand reasoning'}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs text-secondary hover:text-primary"
      >
        <span style={{ color: 'var(--accent-purple)' }}>{open ? '▾' : '▸'}</span>
        <span className="flex-1 truncate">{reasoning.summary}</span>
        <span className="font-mono text-[10px] text-mono">
          {reasoning.node} · {formatTokens(reasoning.tokens)}/{formatTokens(reasoning.budget)} ·{' '}
          {formatDuration(reasoning.durationMs ?? 0)}
        </span>
      </button>
      {open && (
        <div className="border-t border-border-2 px-3 py-2 font-mono text-xs leading-relaxed text-mono">
          {reasoning.text}
        </div>
      )}
    </div>
  );
}
