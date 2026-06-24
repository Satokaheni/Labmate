import { useState } from 'react';
import type { ContextWindow } from '@/types/events';
import { formatTokens } from '@/lib/format';

type SegKey = keyof ContextWindow['segments'];

const SEG_META: Array<{ key: SegKey; label: string; color: string }> = [
  { key: 'systemPrompt', label: 'systemPrompt', color: '#6aa6ff' },
  { key: 'skillInstructions', label: 'skillInstructions', color: '#56c08d' },
  { key: 'conversation', label: 'conversation', color: '#a78bfa' },
  { key: 'workingMemory', label: 'workingMemory', color: '#e0a458' },
  { key: 'reasoning', label: 'reasoning', color: '#8c9bf0' },
];

interface ContextBarProps {
  window: ContextWindow;
  onCompact?: () => void;
  compacting?: boolean;
}

export function ContextBar({ window, onCompact, compacting }: ContextBarProps) {
  const [open, setOpen] = useState(false);

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center justify-between">
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          aria-label={open ? 'Collapse context breakdown' : 'Expand context breakdown'}
          className="flex items-center gap-2 text-left"
        >
          <span className="font-mono text-[11px] uppercase tracking-wide text-mono">Context</span>
          <span data-testid="context-usage" className="font-mono text-[11px] text-secondary">
            {formatTokens(window.used)} / {formatTokens(window.max)}
          </span>
        </button>
        {onCompact && (
          <button
            type="button"
            onClick={onCompact}
            disabled={compacting}
            aria-busy={compacting}
            aria-label={compacting ? 'Compacting context' : 'Compact context'}
            className="font-mono text-[10px] px-1.5 py-0.5 rounded border border-border-2 text-mono hover:text-primary disabled:opacity-40 transition-opacity"
          >
            {compacting ? 'Compacting…' : 'Compact'}
          </button>
        )}
      </div>

      <div className="flex h-2 w-full overflow-hidden rounded-full bg-border-1">
        {SEG_META.map((m) => {
          const pct = window.max > 0 ? (window.segments[m.key] / window.max) * 100 : 0;
          return (
            <div
              key={m.key}
              data-testid="context-segment"
              style={{ width: `${pct}%`, background: m.color }}
            />
          );
        })}
      </div>

      {open && (
        <div className="mt-1 flex flex-col gap-1">
          {SEG_META.map((m) => (
            <div key={m.key} className="flex items-center gap-2 text-[11px]">
              <span className="h-2 w-2 rounded-full" style={{ background: m.color }} />
              <span className="flex-1 text-secondary">{m.label}</span>
              <span className="font-mono text-mono">{formatTokens(window.segments[m.key])}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
