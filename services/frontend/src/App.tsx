import { useState } from 'react';
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

export interface AppProps {
  sessions?: Session[];
  turns?: TurnT[];
  activeSessionId?: string | null;
  agentStatus?: AgentStatus;
  context?: ContextWindow;
  onSend?: (text: string) => void;
  onOpenSession?: (id: string) => void;
  onNewSession?: (mode: Mode) => void;
}

const MODES: Mode[] = ['chat', 'paper', 'code'];

export function App({
  sessions = [],
  turns = [],
  activeSessionId = null,
  agentStatus = EMPTY_STATUS,
  context = EMPTY_CONTEXT,
  onSend = () => {},
  onOpenSession = () => {},
  onNewSession = () => {},
}: AppProps) {
  const [debug, setDebug] = useState(false);
  const [mode, setMode] = useState<Mode>('chat');
  const [previewed, setPreviewed] = useState<Artifact | null>(null);

  const topBar = (
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
  );

  const left = (
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
          onClick={() => onNewSession(mode)}
          className="mb-2 w-full rounded-pill border border-border-2 px-2 py-1.5 text-xs text-secondary hover:text-primary"
        >
          + New session
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-2">
        <SessionList sessions={sessions} activeId={activeSessionId} onOpen={onOpenSession} />
      </div>
      <SystemFooter status={agentStatus} />
    </div>
  );

  const center = (
    <>
      <div className="min-h-0 flex-1 overflow-y-auto px-6 py-4">
        {turns.map((t) => (
          <Turn key={t.id} turn={t} onPreviewArtifact={setPreviewed} />
        ))}
      </div>
      <div className="px-6">
        <ContextBar window={context} />
      </div>
      <Composer
        onSend={onSend}
        onStop={() => {}}
        streaming={agentStatus.brain.state === 'active'}
        node={agentStatus.brain.node}
        thinkingBudget={agentStatus.brain.thinkingBudget}
        contextPct={Math.round((context.used / context.max) * 100)}
      />
    </>
  );

  const right = debug ? (
    <div className="p-4 font-mono text-xs text-mono">
      <div className="mb-2 uppercase tracking-wide">Live trace</div>
      <div className="text-secondary">Debug mode on — node + tool events stream here.</div>
    </div>
  ) : (
    <FilePreview artifact={previewed} />
  );

  return <ChatLayout topBar={topBar} left={left} center={center} right={right} />;
}
