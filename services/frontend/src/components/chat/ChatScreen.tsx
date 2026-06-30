import { useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent } from 'react';
import type {
  LabmateWSStatePublic,
  Session,
  Turn,
  ToolCall,
  Artifact,
  ContextWindow,
} from '@/types/events';
import type { WorkspaceMentionEntry } from '@/config';
import { LabmateMark } from '@/components/LabmateMark';
import { Markdown } from '@/lib/markdown';
import { formatBytes } from '@/lib/format';
import { useWorkspace } from './useWorkspace';
import { WorkspaceRoots } from './WorkspaceRoots';
import { WorkspaceSetupModal } from './WorkspaceSetupModal';
import { MentionPopup } from './MentionPopup';

/* ------------------------------------------------------------------ *
 * Faithful rebuild of Labmate.dc.html (the design comp). The comp is
 * authored entirely in inline styles with literal token hex values, so
 * this screen mirrors those values directly to guarantee a pixel match,
 * while wiring the live WebSocket state in place of the prototype data.
 * ------------------------------------------------------------------ */

type Mode = 'chat' | 'paper' | 'code';

const MODE_META: Record<Mode, { icon: string; label: string; noun: string; chip: string; budget: number }> = {
  chat: { icon: '💬', label: 'Chat', noun: 'chat', chip: '💬 Chat', budget: 1000 },
  paper: { icon: '📄', label: 'Paper writing', noun: 'paper', chip: '📄 Paper writing', budget: 3000 },
  code: { icon: '⌘', label: 'Coding', noun: 'coding', chip: '⌘ Coding', budget: 3000 },
};

function normMode(m?: string): Mode {
  if (m === 'paper' || m === 'code' || m === 'chat') return m;
  return 'chat';
}

function fmtDur(ms?: number): string {
  if (ms == null) return '';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

/** Re-render on an interval while `active` so elapsed timers tick. */
function useNow(active: boolean, intervalMs = 200): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [active, intervalMs]);
  return now;
}

function parseStart(createdAt?: string): number {
  if (createdAt) {
    const t = Date.parse(createdAt);
    if (!Number.isNaN(t)) return t;
  }
  return Date.now();
}

/** Per-call accent: skills green, plain tools blue (mirrors the comp dots). */
function callColor(c: ToolCall): string {
  return c.kind === 'skill' ? '#56c08d' : '#6aa6ff';
}

/** Short file-type badge for an artifact (TS / MD / PY …), matching the comp. */
const LANG_BADGE: Record<string, string> = {
  typescript: 'TS',
  javascript: 'JS',
  tsx: 'TS',
  python: 'PY',
  markdown: 'MD',
  json: 'JSON',
  rust: 'RS',
  yaml: 'YML',
  html: 'HTML',
  css: 'CSS',
};
function langBadge(language: string): string {
  return LANG_BADGE[language.toLowerCase()] ?? language.slice(0, 2).toUpperCase();
}

// ====================================================================
// Static style objects (module-level to avoid rebuild on every render)
// ====================================================================

const TOPBAR_CONTAINER_STYLE: CSSProperties = {
  height: 46,
  flex: 'none',
  display: 'flex',
  alignItems: 'center',
  padding: '0 16px',
  background: '#0a0c10',
  borderBottom: '1px solid #20242c',
  gap: 14,
};

const TOPBAR_DOT_GROUP_STYLE: CSSProperties = {
  display: 'flex',
  gap: 7,
};

const TOPBAR_TITLE_GROUP_STYLE: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  marginLeft: 8,
  flex: 'none',
};

const TOPBAR_TITLE_STYLE: CSSProperties = {
  fontSize: 13.5,
  fontWeight: 600,
  color: '#e6e8ec',
};

const TOPBAR_SLASH_STYLE: CSSProperties = {
  fontSize: 12,
  color: '#5e6671',
};

const TOPBAR_SESSION_TITLE_STYLE: CSSProperties = {
  fontSize: 12.5,
  color: '#939ba7',
  fontWeight: 500,
  maxWidth: 220,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};

const TOPBAR_DIVIDER_STYLE: CSSProperties = {
  width: 1,
  height: 18,
  background: '#20242c',
  flex: 'none',
};

const TOPBAR_WORKSPACE_STYLE: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  minWidth: 0,
  flex: '1 1 auto',
  overflow: 'hidden',
};

const TOPBAR_STATUS_GROUP_STYLE: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 7,
  background: '#13161c',
  border: '1px solid #20242c',
  borderRadius: 8,
  padding: '6px 11px',
};

const TOPBAR_STATUS_DOT_STYLE: CSSProperties = {
  width: 6,
  height: 6,
  borderRadius: '50%',
  background: '#56c08d',
};

const TOPBAR_STATUS_TEXT_STYLE: CSSProperties = {
  fontFamily: "'IBM Plex Mono'",
  fontSize: 11,
  color: '#939ba7',
};

const TOPBAR_TABS_GROUP_STYLE: CSSProperties = {
  display: 'flex',
  background: '#13161c',
  border: '1px solid #20242c',
  borderRadius: 8,
  padding: 3,
  gap: 2,
};

const SIDEBAR_CONTAINER_STYLE: CSSProperties = {
  width: 288,
  flex: 'none',
  background: '#0a0c10',
  borderRight: '1px solid #20242c',
  display: 'flex',
  flexDirection: 'column',
  minHeight: 0,
};

const SIDEBAR_TOP_SECTION_STYLE: CSSProperties = {
  flex: 'none',
  padding: '14px 14px 0',
};

const MODE_TABS_CONTAINER_STYLE: CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 3,
  background: '#0f1115',
  border: '1px solid #20242c',
  borderRadius: 10,
  padding: 4,
  marginBottom: 14,
};

const NEW_SESSION_BUTTON_STYLE: CSSProperties = {
  width: '100%',
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  background: 'transparent',
  border: '1px solid #2a2f39',
  color: '#c7ccd3',
  fontFamily: 'inherit',
  fontSize: 13,
  fontWeight: 500,
  padding: '9px 12px',
  borderRadius: 8,
  cursor: 'pointer',
};

const CHATS_LABEL_STYLE: CSSProperties = {
  fontFamily: "'IBM Plex Mono'",
  fontSize: 10.5,
  letterSpacing: '.12em',
  textTransform: 'uppercase',
  color: '#5e6671',
  margin: '16px 4px 8px',
};

const CHAT_SCROLL_STYLE: CSSProperties = {
  flex: 1,
  minHeight: 0,
  overflowY: 'auto',
  padding: '0 14px 12px',
};

const SIDEBAR_BOTTOM_SECTION_STYLE: CSSProperties = {
  flex: 'none',
  borderTop: '1px solid #20242c',
  background: '#0c0e12',
  padding: '13px 14px',
};

const SYSTEM_LABEL_STYLE: CSSProperties = {
  fontFamily: "'IBM Plex Mono'",
  fontSize: 10,
  letterSpacing: '.12em',
  textTransform: 'uppercase',
  color: '#5e6671',
  marginBottom: 11,
};

const SYSTEM_ROWS_GROUP_STYLE: CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 8,
};

const USER_BUBBLE_CONTAINER_STYLE: CSSProperties = {
  display: 'flex',
  justifyContent: 'flex-end',
  marginBottom: 30,
};

const USER_BUBBLE_STYLE: CSSProperties = {
  maxWidth: '82%',
  background: '#15181e',
  border: '1px solid #232831',
  borderRadius: '15px 15px 5px 15px',
  padding: '12px 16px',
  fontSize: 15,
  lineHeight: 1.62,
  color: '#e6e8ec',
};

const THINKING_LINE_STYLE: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 9,
  marginBottom: 16,
};

const THINKING_DOT_STYLE: CSSProperties = {
  width: 6,
  height: 6,
  borderRadius: '50%',
  background: '#6aa6ff',
  animation: 'pulse 1.4s infinite',
};

const THINKING_TEXT_STYLE: CSSProperties = {
  fontSize: 13.5,
  color: '#939ba7',
  animation: 'pulse 1.8s infinite',
};

const THINKING_TIME_STYLE: CSSProperties = {
  fontFamily: "'IBM Plex Mono'",
  fontSize: 11,
  color: '#5e6671',
};

const ASSISTANT_TURN_CONTAINER_STYLE: CSSProperties = {
  marginBottom: 30,
};

const ASSISTANT_HEADER_STYLE: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  marginBottom: 13,
};

const ASSISTANT_LABEL_STYLE: CSSProperties = {
  fontFamily: "'IBM Plex Mono'",
  fontSize: 10.5,
  letterSpacing: '.1em',
  textTransform: 'uppercase',
  color: '#5e6671',
};

const THOUGHT_CONTAINER_STYLE: CSSProperties = {
  display: 'block',
  width: '100%',
  borderLeft: '2px solid #2a2f39',
  padding: '3px 0 3px 14px',
  marginBottom: 16,
  cursor: 'pointer',
};

const THOUGHT_HEADER_STYLE: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 9,
};

const THOUGHT_TOGGLE_ICON_STYLE: CSSProperties = {
  fontSize: 11,
  color: '#5e6671',
  width: 9,
};

const THOUGHT_TEXT_STYLE: CSSProperties = {
  display: 'block',
  fontSize: 13,
  lineHeight: 1.6,
  color: '#6b727d',
  marginTop: 9,
  paddingRight: 8,
};

const SKILLS_CHIP_STYLE: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 10,
  border: '1px solid #20242c',
  background: '#13161c',
  borderRadius: 10,
  padding: '11px 13px',
  marginBottom: 16,
  cursor: 'pointer',
};

const SKILLS_COUNT_STYLE: CSSProperties = {
  fontSize: 14,
};

const SKILLS_LABEL_STYLE: CSSProperties = {
  fontSize: 12.5,
  color: '#c7ccd3',
  fontWeight: 600,
  whiteSpace: 'nowrap',
};

const SKILLS_LIST_STYLE: CSSProperties = {
  fontFamily: "'IBM Plex Mono'",
  fontSize: 11,
  color: '#6b727d',
  flex: 1,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};

const SKILLS_VIEW_LINK_STYLE: CSSProperties = {
  fontSize: 11.5,
  color: '#6aa6ff',
  fontWeight: 500,
  whiteSpace: 'nowrap',
};

const ANSWER_CONTAINER_STYLE: CSSProperties = {
  marginBottom: 15,
};

const FILE_INFO_STYLE: CSSProperties = {
  display: 'block',
  minWidth: 0,
};

const FILE_NAME_STYLE: CSSProperties = {
  display: 'block',
  fontSize: 13.5,
  fontWeight: 600,
  color: '#e6e8ec',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};

const FILE_META_STYLE: CSSProperties = {
  display: 'block',
  fontFamily: "'IBM Plex Mono'",
  fontSize: 11,
  color: '#6b727d',
  marginTop: 2,
};

const FILE_SPACER_STYLE: CSSProperties = {
  flex: 1,
};

const FILE_PREVIEW_LINK_STYLE: CSSProperties = {
  fontSize: 11.5,
  color: '#6aa6ff',
  fontWeight: 500,
};

const FILE_DOWNLOAD_BUTTON_STYLE: CSSProperties = {
  width: 28,
  height: 28,
  borderRadius: 7,
  background: '#1b1f26',
  border: '1px solid #2a2f39',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  color: '#939ba7',
  fontSize: 13,
};

const PANEL_LABEL_STYLE: CSSProperties = {
  fontFamily: "'IBM Plex Mono'",
  fontSize: 9,
  letterSpacing: '.1em',
  textTransform: 'uppercase',
  color: '#5e6671',
  marginBottom: 5,
};

const CODEBLOCK_BASE_STYLE: CSSProperties = {
  fontFamily: "'IBM Plex Mono'",
  fontSize: 11,
  background: '#0c0e12',
  border: '1px solid #20242c',
  borderRadius: 6,
  padding: '7px 9px',
  marginBottom: 10,
  overflowX: 'auto',
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
};

const FILE_PREVIEW_HEADER_STYLE: CSSProperties = {
  flex: 'none',
  display: 'flex',
  alignItems: 'center',
  gap: 11,
  padding: '12px 14px',
  borderBottom: '1px solid #20242c',
};

const FILE_PREVIEW_BADGE_STYLE: CSSProperties = {
  width: 32,
  height: 32,
  flex: 'none',
  borderRadius: 7,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  fontFamily: "'IBM Plex Mono'",
  fontSize: 10,
  fontWeight: 600,
};

const FILE_PREVIEW_INFO_STYLE: CSSProperties = {
  minWidth: 0,
  flex: 1,
};

const FILE_PREVIEW_NAME_STYLE: CSSProperties = {
  fontSize: 13,
  fontWeight: 600,
  color: '#e6e8ec',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};

const FILE_PREVIEW_PATH_STYLE: CSSProperties = {
  fontFamily: "'IBM Plex Mono'",
  fontSize: 10.5,
  color: '#6b727d',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};

const FILE_PREVIEW_DOWNLOAD_LINK_STYLE: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 7,
  background: '#1b1f26',
  border: '1px solid #2a2f39',
  borderRadius: 8,
  padding: '6px 11px',
  cursor: 'pointer',
  textDecoration: 'none',
};

const FILE_PREVIEW_DOWNLOAD_ICON_STYLE: CSSProperties = {
  fontSize: 12,
  color: '#c7ccd3',
};

const FILE_PREVIEW_DOWNLOAD_TEXT_STYLE: CSSProperties = {
  fontSize: 11.5,
  color: '#c7ccd3',
  fontWeight: 500,
};

const FILE_PREVIEW_METADATA_STYLE: CSSProperties = {
  flex: 'none',
  display: 'flex',
  alignItems: 'center',
  gap: 10,
  padding: '7px 14px',
  borderBottom: '1px solid #20242c',
  background: '#0c0e12',
  fontFamily: "'IBM Plex Mono'",
  fontSize: 10.5,
  color: '#5e6671',
};

const EMPTY_FILES_CONTAINER_STYLE: CSSProperties = {
  height: '100%',
  display: 'flex',
  flexDirection: 'column',
  alignItems: 'center',
  justifyContent: 'center',
  padding: 30,
  textAlign: 'center',
};

const EMPTY_FILES_ICON_STYLE: CSSProperties = {
  width: 48,
  height: 48,
  borderRadius: 12,
  border: '1.5px dashed #2a2f39',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  fontSize: 20,
  color: '#3c434e',
  marginBottom: 16,
};

const EMPTY_FILES_TITLE_STYLE: CSSProperties = {
  fontSize: 13.5,
  fontWeight: 600,
  color: '#939ba7',
  marginBottom: 6,
};

const EMPTY_FILES_DESCRIPTION_STYLE: CSSProperties = {
  fontSize: 12.5,
  lineHeight: 1.55,
  color: '#5e6671',
  maxWidth: 220,
};

const DEBUG_CONSOLE_CONTAINER_STYLE: CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  minHeight: 0,
  height: '100%',
};

const DEBUG_CONSOLE_HEADER_STYLE: CSSProperties = {
  flex: 'none',
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  padding: '13px 16px',
  borderBottom: '1px solid #20242c',
};

const DEBUG_CONSOLE_DOT_STYLE: CSSProperties = {
  width: 6,
  height: 6,
  borderRadius: '50%',
  background: '#56c08d',
  animation: 'pulse 1.4s infinite',
};

const DEBUG_CONSOLE_LABEL_STYLE: CSSProperties = {
  fontFamily: "'IBM Plex Mono'",
  fontSize: 11,
  letterSpacing: '.1em',
  textTransform: 'uppercase',
  color: '#56c08d',
};

const DEBUG_CONSOLE_SCROLL_STYLE: CSSProperties = {
  flex: 1,
  overflowY: 'auto',
  padding: '14px 16px',
};

const DEBUG_INFO_BOX_STYLE: CSSProperties = {
  background: '#0c0e12',
  border: '1px dashed #2a2f39',
  borderRadius: 8,
  padding: 13,
  marginBottom: 13,
  fontSize: 11.5,
  lineHeight: 1.5,
  color: '#5e6671',
};

const DEBUG_LIVE_BOX_STYLE: CSSProperties = {
  background: '#0c0e12',
  border: '1px solid #20242c',
  borderRadius: 8,
  padding: '10px 11px',
  fontFamily: "'IBM Plex Mono'",
  fontSize: 11,
  lineHeight: 1.85,
  color: '#8a93a0',
};

const COMPOSER_SUBMIT_BUTTON_STYLE: CSSProperties = {
  width: 32,
  height: 32,
  borderRadius: 9,
  background: '#6aa6ff',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  color: '#0a0c10',
  fontSize: 15,
  cursor: 'pointer',
};

const composerTextareaStyle = (maxH: number): CSSProperties => ({
  display: 'block',
  width: '100%',
  resize: 'none',
  border: 'none',
  outline: 'none',
  background: 'transparent',
  fontFamily: 'inherit',
  fontSize: 14.5,
  lineHeight: 1.5,
  color: '#e6e8ec',
  maxHeight: maxH,
  overflowY: 'auto',
});

const filePreviewContentStyle = (isCode: boolean): CSSProperties => ({
  flex: 1,
  overflow: 'auto',
  padding: '16px 14px',
  fontFamily: isCode ? "'IBM Plex Mono'" : 'inherit',
  fontSize: isCode ? 12 : 14,
  lineHeight: isCode ? 1.9 : 1.75,
  color: '#cdd2d9',
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
});

// ====================================================================
// Top bar
// ====================================================================/

/** Traffic-light dot + segmented-tab styles — pure, hoisted so they aren't rebuilt per render. */
const dot = (bg: string): CSSProperties => ({ width: 11, height: 11, borderRadius: '50%', background: bg });
const segTab = (active: boolean): CSSProperties => ({
  display: 'flex',
  alignItems: 'center',
  gap: 5,
  fontFamily: "'IBM Plex Mono'",
  fontSize: 11,
  color: active ? '#e6e8ec' : '#8a93a0',
  background: active ? '#1b1f26' : 'transparent',
  boxShadow: active ? 'inset 0 0 0 1px #2f3742' : 'none',
  borderRadius: 6,
  padding: '5px 10px',
  cursor: 'pointer',
});

const sidebarToggleStyle = (collapsed: boolean): CSSProperties => ({
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  width: 26,
  height: 26,
  marginLeft: 4,
  padding: 0,
  borderRadius: 7,
  cursor: 'pointer',
  color: '#939ba7',
  background: collapsed ? '#1b1f26' : 'transparent',
  border: collapsed ? '1px solid #2f3742' : '1px solid transparent',
  font: 'inherit',
});

const debugToggleStyle = (debug: boolean): CSSProperties => ({
  display: 'flex',
  alignItems: 'center',
  gap: 7,
  borderRadius: 8,
  padding: '6px 11px',
  cursor: 'pointer',
  border: `1px solid ${debug ? '#2f4a3a' : '#20242c'}`,
  background: debug ? '#11201a' : '#13161c',
});

const sidebarTab = (m: Mode, mode: Mode): CSSProperties => ({
  display: 'flex',
  alignItems: 'center',
  gap: 10,
  padding: '9px 12px',
  borderRadius: 7,
  fontSize: 13.5,
  fontWeight: m === mode ? 600 : 400,
  color: m === mode ? '#e6e8ec' : '#939ba7',
  background: m === mode ? '#1b1f26' : 'transparent',
  boxShadow: m === mode ? 'inset 0 0 0 1px #2f3742' : 'none',
  cursor: 'pointer',
});

const sysRowDotStyle = (color: string, pulse: boolean): CSSProperties => ({
  width: 6,
  height: 6,
  borderRadius: '50%',
  background: color,
  animation: pulse ? 'pulse 1.6s infinite' : undefined,
});

const sessionItemStyle = (active: boolean): CSSProperties => ({
  display: 'block',
  width: '100%',
  background: active ? '#15181e' : 'transparent',
  borderRadius: 8,
  padding: '11px 12px',
  marginBottom: 3,
  cursor: 'pointer',
});

const SKILL_ROW_TOGGLE_STYLE: CSSProperties = {
  display: 'block',
  width: '100%',
  cursor: 'pointer',
};

const sessionTitleStyle = (active: boolean): CSSProperties => ({
  fontSize: 13.5,
  fontWeight: active ? 600 : 400,
  color: active ? '#e6e8ec' : '#939ba7',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
});

const fileCardStyle = (active: boolean): CSSProperties => ({
  display: 'flex',
  alignItems: 'center',
  gap: 12,
  border: `1px solid ${active ? '#2f4763' : '#2a2f39'}`,
  background: active ? '#101824' : '#13161c',
  borderRadius: 10,
  padding: '12px 14px',
  cursor: 'pointer',
  marginBottom: 8,
});

const fileCardBadgeStyle = (badgeBg: string, badgeBorder: string): CSSProperties => ({
  width: 38,
  height: 38,
  flex: 'none',
  borderRadius: 8,
  background: badgeBg,
  border: `1px solid ${badgeBorder}`,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  fontFamily: "'IBM Plex Mono'",
  fontSize: 11,
  fontWeight: 600,
});

function TopBar(props: {
  sessionTitle: string;
  rightView: 'skills' | 'files' | null;
  debug: boolean;
  roots: string[];
  workspaceAvailable: boolean;
  sidebarCollapsed: boolean;
  onToggleSidebar: () => void;
  onAddRoot: () => void;
  onRemoveRoot: (path: string) => void;
  onSkills: () => void;
  onFiles: () => void;
  onToggleDebug: () => void;
}) {
  const { sessionTitle, rightView, debug, roots, workspaceAvailable, sidebarCollapsed, onToggleSidebar, onAddRoot, onRemoveRoot, onSkills, onFiles, onToggleDebug } = props;


  return (
    <div style={TOPBAR_CONTAINER_STYLE}>
      <div style={TOPBAR_DOT_GROUP_STYLE}>
        <div style={dot('#37404c')} />
        <div style={dot('#37404c')} />
        <div style={dot('#37404c')} />
      </div>
      {/* Sidebar collapse toggle (Claude-Desktop style). A native <button> so it's
          keyboard-accessible (Enter/Space) and focusable for free. */}
      <button
        type="button"
        aria-label={sidebarCollapsed ? 'Show sidebar' : 'Hide sidebar'}
        aria-pressed={sidebarCollapsed}
        title={sidebarCollapsed ? 'Show sidebar' : 'Hide sidebar'}
        onClick={onToggleSidebar}
        style={sidebarToggleStyle(sidebarCollapsed)}
      >
        <svg
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden
        >
          <rect x="3" y="4" width="18" height="16" rx="2" />
          <line x1="9" y1="4" x2="9" y2="20" />
        </svg>
      </button>
      <div style={TOPBAR_TITLE_GROUP_STYLE}>
        <LabmateMark size={18} variant="tile" spin="none" />
        <span style={TOPBAR_TITLE_STYLE}>Labmate</span>
        <span style={TOPBAR_SLASH_STYLE}>/</span>
        <span style={TOPBAR_SESSION_TITLE_STYLE}>
          {sessionTitle}
        </span>
      </div>
      {/* working directories this chat covers (Claude-Desktop style) */}
      <div style={TOPBAR_DIVIDER_STYLE} />
      <div style={TOPBAR_WORKSPACE_STYLE}>
        <WorkspaceRoots roots={roots} available={workspaceAvailable} onAdd={onAddRoot} onRemove={onRemoveRoot} />
      </div>
      <div style={TOPBAR_STATUS_GROUP_STYLE}>
        <span style={TOPBAR_STATUS_DOT_STYLE} />
        <span style={TOPBAR_STATUS_TEXT_STYLE}>llama.cpp :8000</span>
      </div>
      <div style={TOPBAR_TABS_GROUP_STYLE}>
        <button type="button" className="lm-btn" onClick={onSkills} style={segTab(rightView === 'skills')}>
          ⚡ Skills
        </button>
        <button type="button" className="lm-btn" onClick={onFiles} style={segTab(rightView === 'files')}>
          ⎋ Files
        </button>
      </div>
      <button
        type="button"
        className="lm-btn"
        onClick={onToggleDebug}
        style={debugToggleStyle(debug)}
      >
        <span style={{ width: 6, height: 6, borderRadius: '50%', background: debug ? '#56c08d' : '#37404c' }} />
        <span style={{ fontFamily: "'IBM Plex Mono'", fontSize: 11, color: '#939ba7' }}>debug {debug ? 'on' : 'off'}</span>
      </button>
    </div>
  );
}

// ====================================================================
// Left sidebar
// ====================================================================

function sysRow(color: string, label: string, value: string, pulse = false): JSX.Element {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 }}>
      <span style={sysRowDotStyle(color, pulse)} />
      <span style={{ color: '#c7ccd3', flex: 1 }}>{label}</span>
      <span style={{ fontFamily: "'IBM Plex Mono'", fontSize: 10.5, color: '#6b727d' }}>{value}</span>
    </div>
  );
}

function Sidebar(props: {
  mode: Mode;
  sessions: Session[];
  activeSessionId: string | null;
  nodeLabel: string;
  toolCount: number;
  handsSummary: string;
  onSetMode: (m: Mode) => void;
  onNewSession: () => void;
  onOpenSession: (id: string) => void;
}) {
  const { mode, sessions, activeSessionId, nodeLabel, toolCount, handsSummary, onSetMode, onNewSession, onOpenSession } =
    props;

  return (
    <div style={SIDEBAR_CONTAINER_STYLE}>
      <div style={SIDEBAR_TOP_SECTION_STYLE}>
        <div style={MODE_TABS_CONTAINER_STYLE}>
          <button type="button" className="lm-btn" onClick={() => onSetMode('chat')} style={sidebarTab('chat', mode)}>
            <span style={{ fontSize: 14 }}>💬</span>Chat
          </button>
          <button type="button" className="lm-btn" onClick={() => onSetMode('paper')} style={sidebarTab('paper', mode)}>
            <span style={{ fontSize: 14 }}>📄</span>Paper writing
          </button>
          <button type="button" className="lm-btn" onClick={() => onSetMode('code')} style={sidebarTab('code', mode)}>
            <span style={{ fontSize: 14 }}>⌘</span>Coding
          </button>
        </div>
        <button type="button" onClick={onNewSession} style={NEW_SESSION_BUTTON_STYLE}>
          ＋ New {MODE_META[mode].noun} session
        </button>
        <div style={CHATS_LABEL_STYLE}>Chats</div>
      </div>

      <div className="lm-scroll" style={CHAT_SCROLL_STYLE}>
        {sessions.length === 0 && (
          <div style={{ fontSize: 12, color: '#5e6671', padding: '8px 12px' }}>No chats yet.</div>
        )}
        {sessions.map((s) => {
          const active = s.id === activeSessionId;
          const sMode = normMode(s.mode);
          const meta = [MODE_META[sMode].label, s.turnCount != null ? `${s.turnCount} turns` : null]
            .filter(Boolean)
            .join(' · ');
          return (
            <button
              key={s.id}
              type="button"
              className="lm-btn"
              onClick={() => onOpenSession(s.id)}
              style={sessionItemStyle(active)}
            >
              <span style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
                <span style={{ fontSize: 12, opacity: 0.85 }}>{MODE_META[sMode].icon}</span>
                <span style={sessionTitleStyle(active)}>
                  {s.title || `Session ${s.id.slice(0, 6)}`}
                </span>
              </span>
              {meta && (
                <span style={{ display: 'block', fontSize: 11, color: '#5e6671', marginTop: 4, fontFamily: "'IBM Plex Mono'" }}>{meta}</span>
              )}
            </button>
          );
        })}
      </div>

      <div style={SIDEBAR_BOTTOM_SECTION_STYLE}>
        <div style={SYSTEM_LABEL_STYLE}>System</div>
        <div style={SYSTEM_ROWS_GROUP_STYLE}>
          {sysRow('#6aa6ff', 'Brain', nodeLabel, true)}
          {sysRow('#a78bfa', 'MCP bridge', `${toolCount} tools`)}
          {sysRow('#56c08d', 'Hands', handsSummary)}
        </div>
      </div>
    </div>
  );
}

// ====================================================================
// Conversation
// ====================================================================

function UserBubble({ text }: { text: string }) {
  return (
    <div style={USER_BUBBLE_CONTAINER_STYLE}>
      <div style={USER_BUBBLE_STYLE}>
        {text}
      </div>
    </div>
  );
}

function ThinkingLine({ turn, active }: { turn: Turn; active: boolean }) {
  // Lazy ref init: capture the start time once instead of re-parsing every render.
  const startRef = useRef<number | null>(null);
  if (startRef.current === null) startRef.current = parseStart(turn.createdAt);
  const now = useNow(active);
  const elapsed = Math.max(0, now - startRef.current);
  const running = (turn.toolCalls ?? []).find((c) => c.status === 'running');
  const label = running ? `Running ${running.name}` : turn.reasoning ? 'Reasoning' : 'Thinking';

  return (
    <div style={THINKING_LINE_STYLE}>
      <span style={THINKING_DOT_STYLE} />
      <span style={THINKING_TEXT_STYLE}>{label}…</span>
      <span style={THINKING_TIME_STYLE}>{(elapsed / 1000).toFixed(1)}s</span>
    </div>
  );
}

function AssistantTurnView(props: {
  turn: Turn;
  active: boolean;
  thoughtOpen: boolean;
  onToggleThought: () => void;
  onOpenSkills: () => void;
  onOpenArtifact: (a: Artifact) => void;
  activeFileId: string | null;
}) {
  const { turn, active, thoughtOpen, onToggleThought, onOpenSkills, onOpenArtifact, activeFileId } = props;
  const calls = turn.toolCalls ?? [];
  const reasoning = turn.reasoning;

  // The chip is a compact summary — the full per-call log lives in the Skills
  // panel. Dedupe names (a tool called N times shows once) and cap the list so
  // it can't grow unbounded (e.g. run_bash · run_bash · …).
  const skillNames = [...new Set(calls.map((c) => c.name))];
  const CHIP_MAX_NAMES = 4;
  const skillSummary =
    skillNames.slice(0, CHIP_MAX_NAMES).join(' · ') +
    (skillNames.length > CHIP_MAX_NAMES ? ` · +${skillNames.length - CHIP_MAX_NAMES} more` : '');

  return (
    <div style={ASSISTANT_TURN_CONTAINER_STYLE}>
      {/* avatar + label */}
      <div style={ASSISTANT_HEADER_STYLE}>
        <LabmateMark size={18} variant="tile" spin={active ? 'fast' : 'none'} />
        <span style={ASSISTANT_LABEL_STYLE}>Labmate</span>
      </div>

      {/* live thinking indicator (no answer text yet) */}
      {active && !turn.text && <ThinkingLine turn={turn} active={active} />}

      {/* thought block */}
      {reasoning && (
        <button type="button" className="lm-btn" onClick={onToggleThought} style={THOUGHT_CONTAINER_STYLE}>
          <span style={THOUGHT_HEADER_STYLE}>
            <span style={THOUGHT_TOGGLE_ICON_STYLE}>{thoughtOpen ? '▾' : '▸'}</span>
            <span style={{ fontSize: 13, color: '#939ba7' }}>Thought for {fmtDur(reasoning.durationMs) || `${reasoning.tokens} tok`}</span>
            <span style={{ fontFamily: "'IBM Plex Mono'", fontSize: 10.5, color: '#5e6671' }}>
              · {reasoning.node} · {reasoning.tokens.toLocaleString()} / {reasoning.budget.toLocaleString()}
            </span>
          </span>
          {thoughtOpen && <span style={THOUGHT_TEXT_STYLE}>{reasoning.text}</span>}
        </button>
      )}

      {/* skills chip */}
      {calls.length > 0 && (
        <button type="button" className="lm-btn" onClick={onOpenSkills} style={SKILLS_CHIP_STYLE}>
          <span style={SKILLS_COUNT_STYLE}>⚡</span>
          <span style={SKILLS_LABEL_STYLE}>{skillNames.length} skill{skillNames.length === 1 ? '' : 's'} used</span>
          <span style={SKILLS_LIST_STYLE}>{skillSummary}</span>
          <span style={SKILLS_VIEW_LINK_STYLE}>View →</span>
        </button>
      )}

      {/* answer */}
      {turn.text && (
        <div style={ANSWER_CONTAINER_STYLE}>
          <Markdown text={turn.text} />
        </div>
      )}

      {/* artifacts */}
      {(turn.artifacts ?? []).map((a) => (
        <FileCard key={a.id} artifact={a} active={a.id === activeFileId} onClick={() => onOpenArtifact(a)} />
      ))}
    </div>
  );
}

function FileCard({ artifact, active, onClick }: { artifact: Artifact; active: boolean; onClick: () => void }) {
  const isCode = artifact.preview === 'code';
  const badgeBg = isCode ? '#16202e' : '#1b1726';
  const badgeBorder = isCode ? '#243447' : '#332a47';
  const badgeColor = isCode ? '#6aa6ff' : '#a78bfa';
  const badge = langBadge(artifact.language);

  return (
    <button
      type="button"
      className="lm-btn"
      onClick={onClick}
      style={fileCardStyle(active)}
    >
      <span
        style={{ ...fileCardBadgeStyle(badgeBg, badgeBorder), color: badgeColor }}
      >
        {badge}
      </span>
      <span style={FILE_INFO_STYLE}>
        <span style={FILE_NAME_STYLE}>{artifact.name}</span>
        <span style={FILE_META_STYLE}>
          {artifact.language} · {formatBytes(artifact.sizeBytes)} · generated
        </span>
      </span>
      <span style={FILE_SPACER_STYLE} />
      <span style={FILE_PREVIEW_LINK_STYLE}>Preview</span>
      <span style={FILE_DOWNLOAD_BUTTON_STYLE}>
        ⤓
      </span>
    </button>
  );
}

// ====================================================================
// Context strip
// ====================================================================

const CTX_SEGS = [
  { key: 'systemPrompt', label: 'System prompt', color: '#4a5260' },
  { key: 'skillInstructions', label: 'Skill instructions', color: '#5e6671' },
  { key: 'conversation', label: 'Conversation', color: '#6aa6ff' },
  { key: 'workingMemory', label: 'Working memory', color: '#a78bfa' },
  { key: 'reasoning', label: 'Reasoning', color: '#e0a458' },
] as const;

function ContextStrip({ window: ctx, open, onToggle }: { window?: ContextWindow; open: boolean; onToggle: () => void }) {
  const used = ctx?.used ?? 0;
  const max = ctx?.max ?? 0;
  const pct = max > 0 ? Math.round((used / max) * 100) : 0;
  const segs = ctx?.segments;
  const free = ctx ? (ctx.free ?? Math.max(0, max - used)) : 0;
  const w = (v: number) => (max > 0 ? `${(v / max) * 100}%` : '0%');

  return (
    <div style={{ flex: 'none', padding: '0 24px 14px', background: '#0f1115' }}>
      <div style={{ maxWidth: 680, margin: '0 auto' }}>
        <button
          type="button"
          className="lm-btn"
          onClick={onToggle}
          style={{ display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer', marginBottom: 9, width: '100%' }}
        >
          <span
            style={{
              fontFamily: "'IBM Plex Mono'",
              fontSize: 10,
              letterSpacing: '.12em',
              textTransform: 'uppercase',
              color: '#5e6671',
            }}
          >
            Context
          </span>
          <span style={{ flex: 1 }} />
          <span style={{ fontFamily: "'IBM Plex Mono'", fontSize: 10.5, color: '#939ba7' }}>
            {used.toLocaleString()} / {max.toLocaleString()}
          </span>
          <span style={{ fontFamily: "'IBM Plex Mono'", fontSize: 10.5, color: '#6b727d' }}>· {pct}%</span>
          <span style={{ fontSize: 10, color: '#5e6671', width: 9, textAlign: 'center' }}>{open ? '▾' : '▸'}</span>
        </button>
        <div style={{ display: 'flex', height: 7, borderRadius: 4, overflow: 'hidden', background: '#15181e' }}>
          {segs &&
            CTX_SEGS.map((s) => <div key={s.key} style={{ width: w(segs[s.key]), background: s.color }} />)}
          <div style={{ flex: 1, background: '#15181e' }} />
        </div>
        {open && segs && (
          <div style={{ marginTop: 12, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '7px 26px' }}>
            {CTX_SEGS.map((s) => (
              <div key={s.key} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 11.5 }}>
                <span style={{ width: 8, height: 8, borderRadius: 2, background: s.color }} />
                <span style={{ color: '#939ba7', flex: 1 }}>{s.label}</span>
                <span style={{ fontFamily: "'IBM Plex Mono'", fontSize: 10.5, color: '#6b727d' }}>
                  {segs[s.key].toLocaleString()}
                </span>
              </div>
            ))}
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 11.5 }}>
              <span style={{ width: 8, height: 8, borderRadius: 2, background: '#222730', border: '1px solid #2f3742' }} />
              <span style={{ color: '#5e6671', flex: 1 }}>Free</span>
              <span style={{ fontFamily: "'IBM Plex Mono'", fontSize: 10.5, color: '#5e6671' }}>
                {free.toLocaleString()}
              </span>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ====================================================================
// Composer
// ====================================================================

const COMPOSER_MAX_H = 200; // px — grow with content up to here, then scroll

/** Active @-mention token immediately before the caret, or null. */
function detectMention(value: string, caret: number): { start: number; query: string } | null {
  const before = value.slice(0, caret);
  const m = /(^|\s)@([^\s@]*)$/.exec(before);
  if (!m) return null;
  return { start: caret - m[2].length - 1, query: m[2] };
}

function Composer(props: { mode: Mode; budget: number; sessionId: string; onSend: (text: string) => void }) {
  const { mode, budget, sessionId, onSend } = props;
  const api = typeof window !== 'undefined' ? window.electronAPI : undefined;
  const [text, setText] = useState('');
  const taRef = useRef<HTMLTextAreaElement>(null);

  // @-mention autocomplete state
  const [entries, setEntries] = useState<WorkspaceMentionEntry[]>([]);
  const [open, setOpen] = useState(false);
  const [index, setIndex] = useState(0);
  const mentionRef = useRef<{ start: number; query: string } | null>(null);
  const searchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Auto-grow the textarea with its content (up to COMPOSER_MAX_H, then scroll)
  // so multi-line / pasted messages expand the box instead of being clipped.
  useEffect(() => {
    const el = taRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, COMPOSER_MAX_H)}px`;
  }, [text]);

  const updateMention = (value: string, caret: number) => {
    const mention = detectMention(value, caret);
    mentionRef.current = mention;
    if (!mention || !api?.searchWorkspace) {
      setOpen(false);
      setEntries([]);
      return;
    }
    if (searchTimer.current) clearTimeout(searchTimer.current);
    searchTimer.current = setTimeout(() => {
      api
        .searchWorkspace(sessionId, mention.query)
        .then(({ entries: found }) => {
          if (!mentionRef.current) return; // mention dismissed while searching
          setEntries(found);
          setOpen(found.length > 0);
          setIndex(0);
        })
        .catch(() => setOpen(false));
    }, 120);
  };

  const accept = (entry: WorkspaceMentionEntry) => {
    const mention = mentionRef.current;
    const el = taRef.current;
    if (!mention || !el) return;
    const caret = el.selectionStart ?? text.length;
    const before = text.slice(0, mention.start);
    const after = text.slice(caret);
    const insert = `${entry.insert} `;
    const next = before + insert + after;
    setText(next);
    setOpen(false);
    setEntries([]);
    mentionRef.current = null;
    const newCaret = (before + insert).length;
    requestAnimationFrame(() => {
      if (taRef.current) {
        taRef.current.focus();
        taRef.current.setSelectionRange(newCaret, newCaret);
      }
    });
  };

  const submit = () => {
    const t = text.trim();
    if (!t) return;
    onSend(t);
    setText('');
    setOpen(false);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (open && entries.length) {
      if (e.key === 'ArrowDown') { e.preventDefault(); setIndex((i) => (i + 1) % entries.length); return; }
      if (e.key === 'ArrowUp') { e.preventDefault(); setIndex((i) => (i - 1 + entries.length) % entries.length); return; }
      if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); accept(entries[index]); return; }
      if (e.key === 'Escape') { e.preventDefault(); setOpen(false); return; }
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div style={{ flex: 'none', padding: '0 24px 20px', background: '#0f1115' }}>
      <div style={{ maxWidth: 680, margin: '0 auto' }}>
        <div
          style={{
            position: 'relative',
            border: '1px solid #2a2f39',
            borderRadius: 13,
            background: '#15181e',
            padding: '14px 16px',
          }}
        >
          {open && entries.length > 0 && (
            <MentionPopup entries={entries} index={index} onHover={setIndex} onPick={accept} />
          )}
          <textarea
            ref={taRef}
            rows={1}
            aria-label="Message input"
            value={text}
            onChange={(e) => { setText(e.target.value); updateMention(e.target.value, e.target.selectionStart ?? e.target.value.length); }}
            onKeyUp={(e) => {
              if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) {
                const el = e.currentTarget;
                updateMention(el.value, el.selectionStart ?? el.value.length);
              }
            }}
            onKeyDown={onKeyDown}
            placeholder="Reply, paste a spec, or @ a file…"
            className="lm-scroll"
            style={composerTextareaStyle(COMPOSER_MAX_H)}
          />
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 14 }}>
            <span style={{ fontFamily: "'IBM Plex Mono'", fontSize: 11, color: '#5e6671' }}>{MODE_META[mode].chip}</span>
            <span style={{ fontFamily: "'IBM Plex Mono'", fontSize: 11, color: '#5e6671' }}>·</span>
            <span style={{ fontFamily: "'IBM Plex Mono'", fontSize: 11, color: '#5e6671' }}>
              thinking {budget.toLocaleString()}
            </span>
            <div style={{ flex: 1 }} />
            <button type="button" className="lm-btn" onClick={submit} style={COMPOSER_SUBMIT_BUTTON_STYLE}>
              ↑
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ====================================================================
// Right panel: skills timeline / file preview / debug / empty
// ====================================================================

function panelHeader(dot: string, label: string, right?: string, pulse = true): JSX.Element {
  return (
    <div
      style={{
        flex: 'none',
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: '13px 16px',
        borderBottom: '1px solid #20242c',
      }}
    >
      <span
        style={{ width: 6, height: 6, borderRadius: '50%', background: dot, animation: pulse ? 'pulse 1.6s infinite' : undefined }}
      />
      <span
        style={{
          fontFamily: "'IBM Plex Mono'",
          fontSize: 11,
          letterSpacing: '.1em',
          textTransform: 'uppercase',
          color: '#9aa2ad',
        }}
      >
        {label}
      </span>
      {right != null && (
        <>
          <div style={{ flex: 1 }} />
          <span style={{ fontFamily: "'IBM Plex Mono'", fontSize: 10.5, color: '#5e6671' }}>{right}</span>
        </>
      )}
    </div>
  );
}

function SkillsPanel({
  calls,
  openMap,
  onToggle,
}: {
  calls: ToolCall[];
  openMap: Record<string, boolean>;
  onToggle: (id: string) => void;
}) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0, height: '100%' }}>
      {panelHeader('#6aa6ff', 'Skills · this turn', `${calls.length} run`)}
      <div className="lm-scroll" style={{ flex: 1, overflowY: 'auto', padding: '18px 16px' }}>
        {calls.length === 0 && (
          <div style={{ fontSize: 12, color: '#5e6671' }}>No skills ran on the current turn.</div>
        )}
        {calls.map((c, i) => {
          const open = !!openMap[c.id];
          const color = callColor(c);
          const last = i === calls.length - 1;
          return (
            <div key={c.id} style={{ display: 'flex', gap: 12 }}>
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', flex: 'none', paddingTop: 4 }}>
                <span
                  style={{ width: 9, height: 9, borderRadius: '50%', background: color, flex: 'none', boxShadow: `0 0 8px ${color}66` }}
                />
                {!last && <span style={{ width: 1, flex: 1, background: '#20242c', marginTop: 6, minHeight: 16 }} />}
              </div>
              <div style={{ flex: 1, minWidth: 0, paddingBottom: 16 }}>
                <button type="button" className="lm-btn" onClick={() => onToggle(c.id)} style={SKILL_ROW_TOGGLE_STYLE}>
                  <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span style={{ fontFamily: "'IBM Plex Mono'", fontSize: 12.5, fontWeight: 600, color }}>{c.name}</span>
                    <span
                      style={{
                        fontFamily: "'IBM Plex Mono'",
                        fontSize: 9,
                        color: '#5e6671',
                        border: '1px solid #2a2f39',
                        borderRadius: 4,
                        padding: '1px 5px',
                      }}
                    >
                      {c.kind ?? 'tool'}
                    </span>
                    <span style={{ flex: 1 }} />
                    {c.durationMs != null && (
                      <span style={{ fontFamily: "'IBM Plex Mono'", fontSize: 10, color: '#5e6671' }}>{fmtDur(c.durationMs)}</span>
                    )}
                    <span style={{ fontSize: 10, color: '#5e6671', width: 9, textAlign: 'center' }}>{open ? '▾' : '▸'}</span>
                  </span>
                  {c.summary && <span style={{ display: 'block', fontSize: 12, color: '#7e8693', marginTop: 5 }}>{c.summary}</span>}
                </button>
                {open && (
                  <div style={{ marginTop: 12 }}>
                    {c.reasoningWhy && (
                      <>
                        <PanelLabel>LLM reasoning</PanelLabel>
                        <div
                          style={{
                            fontSize: 12,
                            lineHeight: 1.6,
                            color: '#9aa2ad',
                            borderLeft: '2px solid #2f3a48',
                            paddingLeft: 11,
                            marginBottom: 12,
                          }}
                        >
                          {c.reasoningWhy}
                        </div>
                      </>
                    )}
                    {c.args != null && (
                      <>
                        <PanelLabel>Input</PanelLabel>
                        <CodeBlock color="#9ecb8a">{stringify(c.args)}</CodeBlock>
                      </>
                    )}
                    {c.result != null && (
                      <>
                        <PanelLabel>Result</PanelLabel>
                        <CodeBlock color="#8a93a0">{stringify(c.result)}</CodeBlock>
                      </>
                    )}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function PanelLabel({ children }: { children: React.ReactNode }) {
  return <div style={PANEL_LABEL_STYLE}>{children}</div>;
}

function CodeBlock({ children, color }: { children: React.ReactNode; color: string }) {
  return <div style={{ ...CODEBLOCK_BASE_STYLE, color }}>{children}</div>;
}

function stringify(v: unknown): string {
  if (typeof v === 'string') return v;
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

function FilePreviewPanel({ artifact }: { artifact: Artifact }) {
  const isCode = artifact.preview === 'code';
  const badgeBg = isCode ? '#16202e' : '#1b1726';
  const badgeBorder = isCode ? '#243447' : '#332a47';
  const badgeColor = isCode ? '#6aa6ff' : '#a78bfa';
  const badge = langBadge(artifact.language);
  const dir = artifact.path.replace(/[^/]*$/, '');

  return (
    <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0, height: '100%' }}>
      <div style={FILE_PREVIEW_HEADER_STYLE}>
        <div style={{ ...FILE_PREVIEW_BADGE_STYLE, background: badgeBg, border: `1px solid ${badgeBorder}`, color: badgeColor }}>
          {badge}
        </div>
        <div style={FILE_PREVIEW_INFO_STYLE}>
          <div style={FILE_PREVIEW_NAME_STYLE}>{artifact.name}</div>
          <div style={FILE_PREVIEW_PATH_STYLE}>{dir || artifact.path}</div>
        </div>
        {artifact.downloadUrl && (
          <a href={artifact.downloadUrl} download={artifact.name} style={FILE_PREVIEW_DOWNLOAD_LINK_STYLE}>
            <span style={FILE_PREVIEW_DOWNLOAD_ICON_STYLE}>⤓</span>
            <span style={FILE_PREVIEW_DOWNLOAD_TEXT_STYLE}>Download</span>
          </a>
        )}
      </div>
      <div style={FILE_PREVIEW_METADATA_STYLE}>
        <span>{artifact.language}</span>
        <span>·</span>
        {artifact.lineCount != null && (
          <>
            <span>{artifact.lineCount} lines</span>
            <span>·</span>
          </>
        )}
        <span>{formatBytes(artifact.sizeBytes)}</span>
      </div>
      <div
        className="lm-scroll"
        style={filePreviewContentStyle(isCode)}
      >
        {artifact.content}
      </div>
    </div>
  );
}

function EmptyFiles() {
  return (
    <div style={EMPTY_FILES_CONTAINER_STYLE}>
      <div style={EMPTY_FILES_ICON_STYLE}>⤓</div>
      <div style={EMPTY_FILES_TITLE_STYLE}>No files yet</div>
      <div style={EMPTY_FILES_DESCRIPTION_STYLE}>
        When Labmate generates a file, it appears in the chat — open it here to preview and download.
      </div>
    </div>
  );
}

function DebugConsole({ nodeLabel, budget }: { nodeLabel: string; budget: number }) {
  return (
    <div style={DEBUG_CONSOLE_CONTAINER_STYLE}>
      <div style={DEBUG_CONSOLE_HEADER_STYLE}>
        <span style={DEBUG_CONSOLE_DOT_STYLE} />
        <span style={DEBUG_CONSOLE_LABEL_STYLE}>Debug · live trace</span>
      </div>
      <div className="lm-scroll" style={DEBUG_CONSOLE_SCROLL_STYLE}>
        <PanelLabel>Selected call</PanelLabel>
        <div style={DEBUG_INFO_BOX_STYLE}>
          Click any skill in the conversation to inspect its full trace — reasoning tokens, timing, args, and result.
        </div>
        <PanelLabel>Live</PanelLabel>
        <div style={DEBUG_LIVE_BOX_STYLE}>
          <div>
            node <span style={{ color: '#6aa6ff' }}>{nodeLabel}</span>
          </div>
          <div>
            thinking_budget <span style={{ color: '#e0a458' }}>{budget.toLocaleString()}</span>
          </div>
          <div>
            model <span style={{ color: '#c7ccd3' }}>gemma-4-31b</span>
          </div>
        </div>
      </div>
    </div>
  );
}

// ====================================================================
// ChatScreen
// ====================================================================

export interface ChatScreenProps {
  state: LabmateWSStatePublic;
  send: (text: string, sessionId: string) => void;
  newSession?: (mode: string) => void;
  newChat: () => string;
  openSession?: (sessionId: string) => void;
  setDebug: (sessionId: string, enabled: boolean) => void;
}

export function ChatScreen({ state, send, newChat, openSession, setDebug }: ChatScreenProps) {
  const sessions = state.sessions ?? [];
  const allTurns = state.turns ?? [];
  const activeSessionId = state.activeSessionId ?? sessions[0]?.id ?? null;
  const activeSession = sessions.find((s) => s.id === activeSessionId) ?? sessions[0];

  // A fresh active session is guaranteed by the WS hook at boot (see
  // ensureActiveSession), so the view no longer creates one from an effect.

  // Only show the active chat's turns (each turn frame carries its sessionId).
  const turns = activeSessionId
    ? allTurns.filter((t) => !t.sessionId || t.sessionId === activeSessionId)
    : allTurns;

  // Auto-scroll the conversation to the bottom on a new message and as the
  // assistant response streams in — but only when the user is already near the
  // bottom, so scrolling up to read earlier messages isn't yanked back down.
  const convScrollRef = useRef<HTMLDivElement>(null);
  const stickToBottomRef = useRef(true);
  const lastTurn = turns[turns.length - 1];
  const scrollSignal =
    `${turns.length}:${lastTurn?.text?.length ?? 0}:` +
    `${lastTurn?.toolCalls?.length ?? 0}:${lastTurn?.status ?? ''}`;
  useEffect(() => {
    const el = convScrollRef.current;
    if (el && stickToBottomRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [scrollSignal]);

  const [mode, setMode] = useState<Mode>(() => normMode(activeSession?.mode));
  const [rightView, setRightView] = useState<'skills' | 'files' | null>('skills');
  const [debug, setLocalDebug] = useState(false);
  const [activeFileId, setActiveFileId] = useState<string | null>(null);
  const [thoughtOpen, setThoughtOpen] = useState<Record<string, boolean>>({});
  const [skillOpen, setSkillOpen] = useState<Record<string, boolean>>({});
  const [sysOpen, setSysOpen] = useState(false);
  const [skillsTurnId, setSkillsTurnId] = useState<string | null>(null);

  // Collapsible left column (Claude-Desktop style); collapsed state persists.
  const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(() => {
    try {
      return localStorage.getItem('lm.sidebarCollapsed') === '1';
    } catch {
      return false;
    }
  });
  const toggleSidebar = () =>
    setSidebarCollapsed((v) => {
      const next = !v;
      try {
        localStorage.setItem('lm.sidebarCollapsed', next ? '1' : '0');
      } catch {
        /* ignore */
      }
      return next;
    });

  // Workspace is keyed on the effective session id (same id used for sends +
  // tool routing), so the roots shown are exactly what the file tools use.
  const wsId = activeSessionId ?? 'default';
  const { roots, addRoot, removeRoot, refresh: refreshWorkspace, available: workspaceAvailable } = useWorkspace(wsId);
  const [needsWorkspaceSetup, setNeedsWorkspaceSetup] = useState(false);
  const [setupDismissed, setSetupDismissed] = useState(false);

  // After login, if no default workspace has ever been chosen, prompt for one.
  useEffect(() => {
    const api = window.electronAPI;
    if (!api?.hasDefaultWorkspace) return;
    api.hasDefaultWorkspace().then((has) => setNeedsWorkspaceSetup(!has)).catch(() => { /* ignore */ });
  }, []);

  const chooseDefaultWorkspace = async () => {
    const api = window.electronAPI;
    if (!api?.setDefaultWorkspace) return;
    const { path } = await api.setDefaultWorkspace();
    if (path) {
      setNeedsWorkspaceSetup(false);
      refreshWorkspace();
    }
  };

  const agent = state.agentStatus;
  const nodeLabel = agent?.brain?.node ?? '—';
  const toolCount = agent?.nervousSystem?.toolsRegistered ?? 0;
  const handsSkills = agent?.hands?.skills ?? [];
  const activeSkill = handsSkills.find((s) => s.state === 'active');
  const handsSummary = activeSkill?.name ?? `${handsSkills.length} skills`;
  const budget = agent?.brain?.thinkingBudget ?? MODE_META[mode].budget;

  // Skills for the right panel: the chip-selected turn, else the latest turn with tool calls.
  const skillsTurn = useMemo(() => {
    if (skillsTurnId) return turns.find((t) => t.id === skillsTurnId);
    return [...turns].reverse().find((t) => (t.toolCalls?.length ?? 0) > 0);
  }, [turns, skillsTurnId]);
  const panelCalls = skillsTurn?.toolCalls ?? [];

  const activeArtifact = useMemo<Artifact | undefined>(() => {
    if (!activeFileId) return undefined;
    for (const t of turns) {
      for (const a of t.artifacts ?? []) {
        if (a.id === activeFileId) return a;
      }
    }
    return undefined;
  }, [turns, activeFileId]);

  const toggleDebug = () => {
    const next = !debug;
    setLocalDebug(next);
    if (activeSessionId) setDebug(activeSessionId, next);
  };

  const showRight = debug || rightView !== null;

  return (
    <div style={{ height: '100vh', width: '100%', display: 'flex', flexDirection: 'column', background: '#0f1115', color: '#e6e8ec', overflow: 'hidden' }}>
      <TopBar
        sessionTitle={activeSession?.title ?? 'New session'}
        rightView={rightView}
        debug={debug}
        roots={roots}
        workspaceAvailable={workspaceAvailable}
        sidebarCollapsed={sidebarCollapsed}
        onToggleSidebar={toggleSidebar}
        onAddRoot={addRoot}
        onRemoveRoot={removeRoot}
        onSkills={() => setRightView((v) => (v === 'skills' ? null : 'skills'))}
        onFiles={() => setRightView((v) => (v === 'files' ? null : 'files'))}
        onToggleDebug={toggleDebug}
      />

      {needsWorkspaceSetup && !setupDismissed && workspaceAvailable && (
        <WorkspaceSetupModal onChoose={chooseDefaultWorkspace} onSkip={() => setSetupDismissed(true)} />
      )}

      <div style={{ flex: 1, display: 'flex', minHeight: 0 }}>
        {!sidebarCollapsed && (
          <Sidebar
            mode={mode}
            sessions={sessions}
            activeSessionId={activeSessionId}
            nodeLabel={nodeLabel}
            toolCount={toolCount}
            handsSummary={handsSummary}
            onSetMode={setMode}
            onNewSession={() => newChat()}
            onOpenSession={(id) => openSession?.(id)}
          />
        )}

        {/* center conversation */}
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', background: '#0f1115', minWidth: 0 }}>
          <div
            className="lm-scroll"
            ref={convScrollRef}
            onScroll={() => {
              const el = convScrollRef.current;
              if (el) {
                stickToBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
              }
            }}
            style={{ flex: 1, overflowY: 'auto', padding: '34px 0 22px' }}
          >
            <div style={{ maxWidth: 680, margin: '0 auto', padding: '0 24px' }}>
              {turns.length === 0 && (
                <div style={{ fontSize: 14, color: '#5e6671', textAlign: 'center', marginTop: 40 }}>
                  Ask anything to get started.
                </div>
              )}
              {turns.map((t) =>
                t.role === 'user' ? (
                  <UserBubble key={t.id} text={t.text ?? ''} />
                ) : (
                  <AssistantTurnView
                    key={t.id}
                    turn={t}
                    active={t.status === 'streaming'}
                    thoughtOpen={!!thoughtOpen[t.id]}
                    onToggleThought={() => setThoughtOpen((m) => ({ ...m, [t.id]: !m[t.id] }))}
                    onOpenSkills={() => {
                      setSkillsTurnId(t.id);
                      setRightView('skills');
                    }}
                    onOpenArtifact={(a) => {
                      setActiveFileId(a.id);
                      setRightView('files');
                    }}
                    activeFileId={activeFileId}
                  />
                )
              )}
            </div>
          </div>

          <ContextStrip window={state.contextWindow} open={sysOpen} onToggle={() => setSysOpen((o) => !o)} />
          <Composer
            mode={mode}
            budget={budget}
            sessionId={wsId}
            onSend={(text) => send(text, wsId)}
          />
        </div>

        {/* right column */}
        {showRight && (
          <div
            style={{
              width: 380,
              flex: 'none',
              background: '#0a0c10',
              borderLeft: '1px solid #20242c',
              display: 'flex',
              flexDirection: 'column',
              minHeight: 0,
            }}
          >
            {debug ? (
              <DebugConsole nodeLabel={nodeLabel} budget={budget} />
            ) : rightView === 'skills' ? (
              <SkillsPanel
                calls={panelCalls}
                openMap={skillOpen}
                onToggle={(id) => setSkillOpen((m) => ({ ...m, [id]: !m[id] }))}
              />
            ) : activeArtifact ? (
              <FilePreviewPanel artifact={activeArtifact} />
            ) : (
              <EmptyFiles />
            )}
          </div>
        )}
      </div>
    </div>
  );
}
