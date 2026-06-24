import type { AgentStatus } from '@/types/events';

const STATE_COLOR: Record<string, string> = {
  idle: '#939ba7',
  active: '#6aa6ff',
  connected: '#56c08d',
  disconnected: '#e0a458',
  error: '#ff6b6b',
  done: '#56c08d',
};

function Dot({ state }: { state: string }) {
  return <span className="h-1.5 w-1.5 rounded-full" style={{ background: STATE_COLOR[state] ?? '#939ba7' }} />;
}

export function SystemFooter({ status }: { status: AgentStatus }) {
  return (
    <div className="flex flex-col gap-1.5 border-t border-border-1 px-3 py-3 font-mono text-[11px] text-mono">
      <div data-testid="footer-brain" className="flex items-center gap-2">
        <Dot state={status.brain.state} />
        <span className="text-secondary">Brain</span>
        <span className="truncate">{status.brain.model} · {status.brain.node}</span>
      </div>
      <div data-testid="footer-nervous" className="flex items-center gap-2">
        <Dot state={status.nervousSystem.state} />
        <span className="text-secondary">{status.nervousSystem.name}</span>
        <span>{status.nervousSystem.toolsRegistered} tools</span>
      </div>
      <div data-testid="footer-hands" className="flex items-center gap-2">
        <Dot state={status.hands.skills.some((s) => s.state === 'active') ? 'active' : 'idle'} />
        <span className="text-secondary">Hands</span>
        <span>{status.hands.skills.length} skills</span>
      </div>
    </div>
  );
}
