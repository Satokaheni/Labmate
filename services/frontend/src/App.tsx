import { useEffect, useMemo, useRef, useState } from 'react';
import { ChatLayout } from '@/layouts/ChatLayout';
import { LabmateMark } from '@/components/LabmateMark';
import { SessionList } from '@/components/SessionList';
import { Turn } from '@/components/Turn';
import { Composer } from '@/components/Composer';
import { ContextBar } from '@/components/ContextBar';
import { SystemFooter } from '@/components/SystemFooter';
import { FilePreview } from '@/components/FilePreview';
import type { AgentStatus, Artifact, ContextWindow, Mode, Session, Turn as TurnT } from '@/types/events';

const EMPTY_STATUS: AgentStatus = {
  brain: { model: 'gemma-31b', endpoint: ':8000', state: 'idle', node: 'chat_node', thinkingBudget: 2000 },
  nervousSystem: { name: 'MCP bridge', transport: 'stdio', state: 'connected', toolsRegistered: 0 },
  hands: { skills: [] },
};

const EMPTY_CONTEXT: ContextWindow = {
  max: 16384, used: 0, free: 16384,
  segments: { systemPrompt: 0, skillInstructions: 0, conversation: 0, workingMemory: 0, reasoning: 0 },
};

const EMPTY_SESSIONS: Session[] = [];
const EMPTY_TURNS: TurnT[] = [];

export interface AppProps {
  sessions?: Session[];
  turns?: TurnT[];
  activeSessionId?: string | null;
  agentStatus?: AgentStatus;
  context?: ContextWindow;
  onSend?: (text: string) => void;
  onStop?: () => void;
  onOpenSession?: (id: string) => void;
  onNewSession?: (mode: Mode) => void;
  onCompact?: () => void;
  compacting?: boolean;
}

const MODES: Mode[] = ['chat', 'paper', 'code'];

export function App({
  sessions = EMPTY_SESSIONS,
  turns = EMPTY_TURNS,
  activeSessionId = null,
  agentStatus = EMPTY_STATUS,
  context = EMPTY_CONTEXT,
  onSend = () => {},
  onStop,
  onOpenSession = () => {},
  onNewSession = () => {},
  onCompact,
  compacting = false,
}: AppProps) {
  const [debug, setDebug] = useState(false);
  const [mode, setMode] = useState<Mode>('chat');
  const [previewed, setPreviewed] = useState<Artifact | null>(null);
  const pendingSendRef = useRef<string | null>(null);

  // When a new session is auto-created in response to the user sending with no active session,
  // fire the buffered message as soon as activeSessionId becomes available.
  useEffect(() => {
    if (activeSessionId && pendingSendRef.current) {
      const text = pendingSendRef.current;
      pendingSendRef.current = null;
      onSendRef.current(text);
    }
  }, [activeSessionId]);

  // Refs keep callbacks stable so useMemos don't re-run when parent re-creates functions.
  const onSendRef = useRef(onSend);
  onSendRef.current = onSend;
  const onStopRef = useRef(onStop);
  onStopRef.current = onStop;
  const onOpenSessionRef = useRef(onOpenSession);
  onOpenSessionRef.current = onOpenSession;
  const onNewSessionRef = useRef(onNewSession);
  onNewSessionRef.current = onNewSession;
  const onCompactRef = useRef(onCompact);
  onCompactRef.current = onCompact;

  // Boolean dep: only re-run center when compact goes from undefined ↔ defined.
  const compactEnabled = onCompact !== undefined;

  const topBar = useMemo(() => (
    <div className="flex w-full items-center gap-3">
      <LabmateMark size={18} variant="tile" spin="none" />
      <span className="text-sm font-semibold tracking-[-0.03em]">Labmate</span>
      <span className="font-mono text-[11px] text-mono">/ {mode}</span>
      <div className="ml-auto flex items-center gap-3">
        <span className="flex items-center gap-1.5 font-mono text-[11px] text-mono">
          <span className="h-2 w-2 rounded-full" style={{ background: 'var(--accent-green)' }} />
          healthy
        </span>
        <button
          type="button"
          onClick={() => setDebug((d) => !d)}
          aria-pressed={debug}
          className="rounded-pill border border-border-2 px-2 py-0.5 font-mono text-[11px] text-secondary hover:text-primary"
        >
          debug {debug ? 'on' : 'off'}
        </button>
      </div>
    </div>
  ), [debug, mode]);

  const left = useMemo(() => (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex gap-1 p-3">
        {MODES.map((m) => (
          <button
            key={m}
            type="button"
            onClick={() => setMode(m)}
            className={[
              'flex-1 rounded-pill px-2 py-1 text-xs',
              m === mode ? 'bg-panel text-primary' : 'text-secondary hover:bg-panel/60',
            ].join(' ')}
          >
            {m}
          </button>
        ))}
      </div>
      <div className="px-3">
        <button
          type="button"
          onClick={() => onNewSessionRef.current(mode)}
          className="mb-2 w-full rounded-pill border border-border-2 px-2 py-1.5 text-xs text-secondary hover:text-primary"
        >
          + New session
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-2">
        <SessionList sessions={sessions} activeId={activeSessionId} onOpen={(id) => onOpenSessionRef.current(id)} />
      </div>
      <SystemFooter status={agentStatus} />
    </div>
  ), [mode, sessions, activeSessionId, agentStatus]);

  const center = useMemo(() => (
    <>
      <div aria-live="polite" aria-label="Conversation" className="min-h-0 flex-1 overflow-y-auto px-6 py-4">
        {turns.map((t) => (
          <Turn key={t.id} turn={t} onPreviewArtifact={setPreviewed} />
        ))}
      </div>
      <Composer
        onSend={(t) => {
          if (activeSessionId) {
            onSendRef.current(t);
          } else {
            pendingSendRef.current = t;
            onNewSessionRef.current(mode);
          }
        }}
        onStop={() => { onStopRef.current?.(); }}
        streaming={agentStatus.brain.state === 'active'}
        node={agentStatus.brain.node}
        thinkingBudget={agentStatus.brain.thinkingBudget}
        contextPct={context.max > 0 ? Math.round((context.used / context.max) * 100) : 0}
      />
      <div className="flex justify-end px-4 pb-2">
        <ContextBar window={context} onCompact={compactEnabled ? () => { onCompactRef.current?.(); } : undefined} compacting={compacting} />
      </div>
    </>
  ), [turns, context, compactEnabled, compacting, agentStatus, activeSessionId, mode]);

  const right = useMemo(() => {
    if (debug) {
      return (
        <div className="p-4 font-mono text-xs text-mono">
          <div className="mb-2 uppercase tracking-wide">Live trace</div>
          <div className="text-secondary">Debug mode on — node + tool events stream here.</div>
        </div>
      );
    }
    if (!previewed) return null;
    return <FilePreview artifact={previewed} />;
  }, [debug, previewed]);

  return <ChatLayout topBar={topBar} left={left} center={center} right={right} />;
}
