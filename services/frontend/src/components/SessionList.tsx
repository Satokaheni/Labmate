import { useMemo, useRef, useState } from 'react';
import type { Mode, Session } from '@/types/events';

export interface SessionListProps {
  sessions: Session[];
  activeId: string | null;
  onOpen: (id: string) => void;
  onDelete?: (id: string) => void;
  onRename?: (id: string, title: string) => void;
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

export function SessionList({ sessions, activeId, onOpen, onDelete, onRename }: SessionListProps) {
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editValue, setEditValue] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);

  const sorted = useMemo(
    () => [...sessions].sort((a, b) => new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime()),
    [sessions],
  );

  const startEdit = (s: Session, e: React.MouseEvent) => {
    e.stopPropagation();
    setEditingId(s.id);
    setEditValue(s.title);
    setTimeout(() => inputRef.current?.select(), 0);
  };

  const commitEdit = (id: string) => {
    const trimmed = editValue.trim();
    if (trimmed) onRename?.(id, trimmed);
    setEditingId(null);
  };

  return (
    <ul className="flex flex-col gap-0.5 overflow-y-auto">
      {sorted.map((s) => {
        const active = s.id === activeId;
        const hovered = s.id === hoveredId;
        const editing = s.id === editingId;
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
              onClick={() => !editing && onOpen(s.id)}
              className={[
                'flex w-full items-center gap-2 rounded-pill px-2.5 py-2 text-left text-sm transition',
                active ? 'bg-panel text-primary' : 'text-secondary hover:bg-panel/60',
              ].join(' ')}
            >
              <ModeIcon mode={s.mode} />
              {editing ? (
                <input
                  ref={inputRef}
                  data-testid="session-rename-input"
                  aria-label={`Rename session: ${s.title}`}
                  value={editValue}
                  onChange={(e) => setEditValue(e.target.value)}
                  onBlur={() => commitEdit(s.id)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') { e.preventDefault(); commitEdit(s.id); }
                    if (e.key === 'Escape') setEditingId(null);
                  }}
                  className="min-w-0 flex-1 bg-transparent text-sm text-primary outline-none"
                  onClick={(e) => e.stopPropagation()}
                />
              ) : (
                <span className="min-w-0 flex-1 truncate">{s.title}</span>
              )}
              <span className="font-mono text-[10px] text-mono">{s.turnCount}</span>
            </button>

            {hovered && !editing && (
              <div className="absolute right-1 top-1/2 flex -translate-y-1/2 gap-0.5">
                {onRename && (
                  <button
                    type="button"
                    data-testid="session-rename"
                    aria-label={`Rename session ${s.title}`}
                    onClick={(e) => startEdit(s, e)}
                    className="rounded px-1.5 py-0.5 text-[11px] text-mono hover:bg-panel hover:text-primary transition"
                  >
                    ✎
                  </button>
                )}
                {onDelete && (
                  <button
                    type="button"
                    data-testid="session-delete"
                    aria-label={`Delete session ${s.title}`}
                    onClick={(e) => {
                      e.stopPropagation();
                      onDelete(s.id);
                    }}
                    className="rounded px-1.5 py-0.5 text-[11px] text-mono hover:bg-panel hover:text-[#ff6b6b] transition"
                  >
                    ✕
                  </button>
                )}
              </div>
            )}
          </li>
        );
      })}
    </ul>
  );
}
