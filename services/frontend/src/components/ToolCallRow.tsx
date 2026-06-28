import { useState } from 'react';
import type { ToolCall } from '@/types/events';
import { formatDuration } from '@/lib/format';

const STATUS_COLOR: Record<NonNullable<ToolCall['status']>, string> = {
  running: '#8c9bf0',
  done: '#6aa6ff',
  error: '#ff6b6b',
};

function pretty(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export function ToolCallRow({ toolCall }: { toolCall: ToolCall }) {
  const [open, setOpen] = useState(false);
  const color = STATUS_COLOR[toolCall.status ?? 'running'];

  return (
    <div className="rounded-pill border border-border-2 bg-page/30">
      <button
        type="button"
        data-testid="tool-call-row"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs"
      >
        {toolCall.status === 'running' ? (
          <span
            data-testid="tool-spinner"
            className="h-3 w-3 animate-spin rounded-full border-2 border-t-transparent"
            style={{ borderColor: color, borderTopColor: 'transparent' }}
          />
        ) : (
          <span aria-hidden style={{ color }}>
            {toolCall.status === 'error' ? '✗' : '✓'}
          </span>
        )}
        <span className="font-mono" style={{ color }}>
          {toolCall.name}
        </span>
        <span className="min-w-0 flex-1 truncate text-secondary">{toolCall.summary}</span>
        <span className="font-mono text-[10px] text-mono">{formatDuration(toolCall.durationMs ?? 0)}</span>
        <span className="text-mono">{open ? '▾' : '▸'}</span>
      </button>

      {open && (
        <div className="space-y-2 border-t border-border-2 px-3 py-2 text-xs">
          <div>
            <div className="mb-1 font-mono text-[10px] uppercase tracking-wide text-mono">why</div>
            <div className="text-secondary">{toolCall.reasoningWhy}</div>
          </div>
          <div>
            <div className="mb-1 font-mono text-[10px] uppercase tracking-wide text-mono">args</div>
            <pre className="overflow-x-auto rounded bg-page p-2 font-mono text-[11px] text-primary-alt">
              {pretty(toolCall.args)}
            </pre>
          </div>
          <div>
            <div className="mb-1 font-mono text-[10px] uppercase tracking-wide text-mono">result</div>
            <pre className="overflow-x-auto rounded bg-page p-2 font-mono text-[11px] text-primary-alt">
              {pretty(toolCall.result)}
            </pre>
          </div>
        </div>
      )}
    </div>
  );
}
