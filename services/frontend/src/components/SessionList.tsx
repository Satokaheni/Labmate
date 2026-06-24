import { useMemo } from 'react';
import type { Mode, Session } from '@/types/events';

export interface SessionListProps {
  sessions: Session[];
  activeId: string | null;
  onOpen: (id: string) => void;
}

const MODE_GLYPH: Record<Mode, string> = {
  chat: '💬',
  paper: '📄',
  code: '⌘',
};

function ModeIcon({ mode }: { mode: Mode }) {
  return (
    <span data-testid={`mode-icon-${mode}`} className="font-mono text-xs text-mono" aria-hidden>
      {MODE_GLYPH[mode]}
    </span>
  );
}

export function SessionList({ sessions, activeId, onOpen }: SessionListProps) {
  const sorted = useMemo(
    () => [...sessions].sort((a, b) => new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime()),
    [sessions],
  );

  return (
    <ul className="flex flex-col gap-0.5 overflow-y-auto">
      {sorted.map((s) => {
        const active = s.id === activeId;
        return (
          <li key={s.id}>
            <button
              type="button"
              data-testid="session-item"
              data-active={active}
              onClick={() => onOpen(s.id)}
              className={[
                'flex w-full items-center gap-2 rounded-pill px-2.5 py-2 text-left text-sm transition',
                active ? 'bg-panel text-primary' : 'text-secondary hover:bg-panel/60',
              ].join(' ')}
            >
              <ModeIcon mode={s.mode} />
              <span className="min-w-0 flex-1 truncate">{s.title}</span>
              <span className="font-mono text-[10px] text-mono">{s.turnCount}</span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
