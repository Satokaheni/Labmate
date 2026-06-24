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

  const usedPct = window.max > 0 ? Math.round((window.used / window.max) * 100) : 0;

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
          {compacting ? (
            <span data-testid="context-compacting" className="font-mono text-[11px] text-[var(--accent-amber)] [animation:breathe_1.5s_ease-in-out_infinite]">
              Compacting… {usedPct}%
            </span>
          ) : (
            <span data-testid="context-usage" className="font-mono text-[11px] text-secondary">
              {formatTokens(window.used)} / {formatTokens(window.max)}
            </span>
          )}
        </button>
        {onCompact && !compacting && (
          <button
            type="button"
            onClick={onCompact}
            disabled={compacting}
            aria-busy={compacting}
            aria-label="Compact context"
            className="font-mono text-[10px] px-1.5 py-0.5 rounded border border-border-2 text-mono hover:text-primary disabled:opacity-40 transition-opacity"
          >
            Compact
          </button>
        )}
      </div>

      {compacting ? (
        <div className="relative h-2 w-full overflow-hidden rounded-full bg-border-1">
          <progress
            data-testid="compact-fill"
            value={usedPct}
            max={100}
            aria-label={`Context usage: ${usedPct}%`}
            className="block h-full w-full appearance-none [&::-moz-progress-bar]:[background:var(--accent-amber)] [&::-webkit-progress-bar]:bg-transparent [&::-webkit-progress-value]:rounded-full [&::-webkit-progress-value]:transition-[width] [&::-webkit-progress-value]:duration-700 [&::-webkit-progress-value]:[background:var(--accent-amber)]"
          />
          <div
            className="pointer-events-none absolute inset-y-0 w-1/4 [animation:compact-scan_1.8s_ease-in-out_infinite]"
            style={{ background: 'linear-gradient(90deg, transparent, rgba(255,200,80,.55), transparent)' }}
          />
        </div>
      ) : (
        <div className="flex h-2 w-full overflow-hidden rounded-full bg-border-1" aria-label={`Context: ${usedPct}% used`}>
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
      )}

      {open && !compacting && (
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
