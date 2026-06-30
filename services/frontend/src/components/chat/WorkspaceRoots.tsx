import type { CSSProperties } from 'react';
import { basename } from './paths';

// ====================================================================
// Static style objects (module-level to avoid rebuild on every render)
// ====================================================================

const WORKSPACE_UNAVAILABLE_STYLE: CSSProperties = {
  fontFamily: "'IBM Plex Mono'",
  fontSize: 11,
  color: '#5e6671',
};

const WORKSPACE_CONTAINER_STYLE: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  minWidth: 0,
  overflow: 'hidden',
};

const WORKSPACE_EMPTY_STYLE: CSSProperties = {
  fontFamily: "'IBM Plex Mono'",
  fontSize: 11,
  color: '#5e6671',
};

const WORKSPACE_ROOT_ICON_STYLE: CSSProperties = {
  opacity: 0.85,
};

const WORKSPACE_ROOT_NAME_STYLE: CSSProperties = {
  overflow: 'hidden',
  textOverflow: 'ellipsis',
};

const WORKSPACE_ROOT_REMOVE_ICON_STYLE: CSSProperties = {
  cursor: 'pointer',
  color: '#5e6671',
  paddingLeft: 1,
};

const WORKSPACE_ADD_BUTTON_STYLE: CSSProperties = {
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
};

const rootChipStyle = (isPrimary: boolean): CSSProperties => ({
  display: 'inline-flex',
  alignItems: 'center',
  gap: 5,
  fontFamily: "'IBM Plex Mono'",
  fontSize: 11,
  color: isPrimary ? '#c7ccd3' : '#939ba7',
  background: '#13161c',
  border: `1px solid ${isPrimary ? '#2f3a48' : '#20242c'}`,
  borderRadius: 7,
  padding: '3px 7px',
  maxWidth: 160,
  whiteSpace: 'nowrap',
});

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
      <span style={WORKSPACE_UNAVAILABLE_STYLE} title="Desktop app only">
        📁 local only
      </span>
    );
  }

  return (
    <div style={WORKSPACE_CONTAINER_STYLE}>
      {roots.length === 0 && <span style={WORKSPACE_EMPTY_STYLE}>no workspace</span>}
      {roots.map((root, i) => (
        <span
          key={root}
          title={root + (i === 0 ? '  (primary — relative paths resolve here)' : '')}
          style={rootChipStyle(i === 0)}
        >
          <span aria-hidden style={WORKSPACE_ROOT_ICON_STYLE}>📁</span>
          <span style={WORKSPACE_ROOT_NAME_STYLE}>{basename(root)}</span>
          <span
            role="button"
            aria-label={`Remove ${basename(root)}`}
            onClick={(e) => { e.stopPropagation(); onRemove(root); }}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onRemove(root); } }}
            tabIndex={0}
            style={WORKSPACE_ROOT_REMOVE_ICON_STYLE}
          >
            ×
          </span>
        </span>
      ))}
      <span
        role="button"
        aria-label="Add directory"
        onClick={onAdd}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onAdd(); } }}
        tabIndex={0}
        title="Add a directory to this chat"
        style={WORKSPACE_ADD_BUTTON_STYLE}
      >
        ＋
      </span>
    </div>
  );
}
