import { basename } from './paths';

/**
 * Top-bar display of a chat's workspace roots (Claude-Desktop style): the
 * directories this conversation can read/write. roots[0] is the primary.
 */
export function WorkspaceRoots({
  roots,
  available,
  onAdd,
  onRemove,
}: {
  roots: string[];
  available: boolean;
  onAdd: () => void;
  onRemove: (path: string) => void;
}) {
  if (!available) {
    return (
      <span style={{ fontFamily: "'IBM Plex Mono'", fontSize: 11, color: '#5e6671' }} title="Desktop app only">
        📁 local only
      </span>
    );
  }

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, minWidth: 0, overflow: 'hidden' }}>
      {roots.length === 0 && (
        <span style={{ fontFamily: "'IBM Plex Mono'", fontSize: 11, color: '#5e6671' }}>no workspace</span>
      )}
      {roots.map((root, i) => (
        <span
          key={root}
          title={root + (i === 0 ? '  (primary — relative paths resolve here)' : '')}
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: 5,
            fontFamily: "'IBM Plex Mono'",
            fontSize: 11,
            color: i === 0 ? '#c7ccd3' : '#939ba7',
            background: '#13161c',
            border: `1px solid ${i === 0 ? '#2f3a48' : '#20242c'}`,
            borderRadius: 7,
            padding: '3px 7px',
            maxWidth: 160,
            whiteSpace: 'nowrap',
          }}
        >
          <span aria-hidden style={{ opacity: 0.85 }}>📁</span>
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{basename(root)}</span>
          <span
            role="button"
            aria-label={`Remove ${basename(root)}`}
            onClick={(e) => { e.stopPropagation(); onRemove(root); }}
            style={{ cursor: 'pointer', color: '#5e6671', paddingLeft: 1 }}
          >
            ×
          </span>
        </span>
      ))}
      <span
        role="button"
        aria-label="Add directory"
        onClick={onAdd}
        title="Add a directory to this chat"
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          width: 20,
          height: 20,
          borderRadius: 6,
          border: '1px solid #2a2f39',
          color: '#939ba7',
          fontSize: 13,
          cursor: 'pointer',
          flex: 'none',
        }}
      >
        ＋
      </span>
    </div>
  );
}
