import { ToolCallRow } from './ToolCallRow';
import type { Turn as TurnT } from '@/types/events';

export interface ToolCallPanelProps {
  turns: TurnT[];
}

export function ToolCallPanel({ turns }: ToolCallPanelProps) {
  const assistantTurns = turns.filter(
    (t) => t.role === 'assistant' && t.toolCalls && t.toolCalls.length > 0,
  );

  if (assistantTurns.length === 0) {
    return (
      <div className="flex h-full items-center justify-center p-4 font-mono text-xs text-mono">
        No tool calls yet
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col overflow-y-auto p-3">
      <div className="mb-3 font-mono text-[11px] uppercase tracking-wide text-mono">Tool calls</div>
      <div className="flex flex-col gap-4">
        {assistantTurns.map((t) => (
          <div key={t.id} className="flex flex-col gap-1">
            <div className="mb-1 font-mono text-[10px] text-mono opacity-60">
              turn {t.id.slice(-6)}
            </div>
            {(t.toolCalls ?? []).map((tc) => (
              <ToolCallRow key={tc.id} toolCall={tc} />
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}
