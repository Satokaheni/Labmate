import { useMemo, useState } from 'react';
import type { Mode, Session } from '@/types/events';

export interface SessionListProps {
  sessions: Session[];
  activeId: string | null;
  onOpen: (id: string) => void;
  onDelete?: (id: string) => void;
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

export function SessionList({ sessions, activeId, onOpen, onDelete }: SessionListProps) {
  const [hoveredId, setHoveredId] = useState<string | null>(null);

  const sorted = useMemo(
    () => [...sessions].sort((a, b) => new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime()),
    [sessions],
  );

  return (
    <ul className="flex flex-col gap-0.5 overflow-y-auto">
      {sorted.map((s) => {
        const active = s.id === activeId;
        const hovered = s.id === hoveredId;
        return (
          <li
            key={s.id}
            className="relative"
            onMouseEnter={() => setHoveredId(s.id)}
            onMouseLeave={() => setHoveredId(null)}
          >
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

            {hovered && onDelete && (
              <button
                type="button"
                data-testid="session-delete"
                aria-label={`Delete session ${s.title}`}
                onClick={(e) => {
                  e.stopPropagation();
                  onDelete(s.id);
                }}
                className="absolute right-2 top-1/2 -translate-y-1/2 rounded px-1.5 py-0.5 text-[11px] text-mono hover:bg-panel hover:text-[#ff6b6b] transition"
              >
                X
              </button>
            )}
          </li>
        );
      })}
    </ul>
  );
}
