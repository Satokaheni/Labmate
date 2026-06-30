import type { WorkspaceMentionEntry } from '@/config';
import { basename } from './paths';

/** Dropdown of file/dir matches for the composer @-mention autocomplete. */
export function MentionPopup({
  entries,
  index,
  onHover,
  onPick,
}: {
  entries: WorkspaceMentionEntry[];
  index: number;
  onHover: (i: number) => void;
  onPick: (entry: WorkspaceMentionEntry) => void;
}) {
  return (
    <div
      className="lm-scroll"
      style={{
        position: 'absolute',
        left: 0,
        right: 0,
        bottom: 'calc(100% + 6px)',
        maxHeight: 240,
        overflowY: 'auto',
        background: '#0c0e12',
        border: '1px solid #2a2f39',
        borderRadius: 10,
        padding: 4,
        boxShadow: '0 12px 32px rgba(0,0,0,0.45)',
        zIndex: 30,
      }}
    >
      {entries.map((e, i) => (
        <div
          key={e.absolute}
          role="button"
          onMouseEnter={() => onHover(i)}
          onMouseDown={(ev) => { ev.preventDefault(); onPick(e); }}
          onKeyDown={(ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); onPick(e); } }}
          tabIndex={-1}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 9,
            padding: '6px 9px',
            borderRadius: 7,
            cursor: 'pointer',
            background: i === index ? '#1b1f26' : 'transparent',
          }}
        >
          <span aria-hidden style={{ fontSize: 12 }}>{e.isDir ? '📁' : '📄'}</span>
          <span style={{ fontSize: 13, color: '#e6e8ec', whiteSpace: 'nowrap' }}>{basename(e.display)}</span>
          <span
            style={{
              fontFamily: "'IBM Plex Mono'",
              fontSize: 10.5,
              color: '#5e6671',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {e.display}
          </span>
        </div>
      ))}
    </div>
  );
}
