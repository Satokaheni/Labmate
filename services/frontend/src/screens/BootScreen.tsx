import { LabmateMark } from '@/components/LabmateMark';
import { RegressionPlot } from './RegressionPlot';
import type { Subsystem, SubsystemId, SubsystemState } from '@/types/events';

export interface BootScreenProps {
  subsystems: Subsystem[];
  onRetry: (id: SubsystemId) => void;
}

const DOT_COLOR: Record<SubsystemId, string> = {
  brain: '#6aa6ff',
  nervous_system: '#a78bfa',
  hands: '#56c08d',
  memory: '#e0a458',
  workspace: '#939ba7',
};

function statusText(state: SubsystemState): string {
  switch (state) {
    case 'pending':
      return 'queued';
    case 'starting':
      return 'starting…';
    case 'ready':
      return 'done';
    case 'degraded':
      return 'degraded';
    case 'failed':
      return 'failed';
  }
}

function SubsystemRow({ s, onRetry }: { s: Subsystem; onRetry: (id: SubsystemId) => void }) {
  return (
    <div
      data-testid="subsystem-row"
      className="flex items-center gap-3 border-b border-border-1 py-3 last:border-b-0"
    >
      <span className="h-2.5 w-2.5 rounded-full" style={{ background: DOT_COLOR[s.id] }} />
      <div className="flex min-w-0 flex-1 flex-col">
        <span className="text-sm text-primary">{s.label}</span>
        <span className="font-mono text-[11px] text-mono">{s.message ?? s.detail}</span>
      </div>
      <div className="flex items-center gap-2">
        {s.state === 'starting' && (
          <span
            data-testid={`row-spinner-${s.id}`}
            className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-secondary border-t-transparent"
          />
        )}
        {s.state === 'ready' && (
          <span data-testid={`row-check-${s.id}`} className="text-sm" style={{ color: 'var(--accent-green)' }}>
            ✓
          </span>
        )}
        {s.state === 'failed' && (
          <span className="text-sm" style={{ color: '#ff6b6b' }}>
            ✗
          </span>
        )}
        <span className="font-mono text-[11px] text-mono">{statusText(s.state)}</span>
        {s.state === 'failed' && s.required && (
          <button
            type="button"
            onClick={() => onRetry(s.id)}
            className="rounded-pill border border-border-3 px-2 py-0.5 font-mono text-[11px] text-secondary hover:text-primary"
          >
            Retry
          </button>
        )}
      </div>
    </div>
  );
}

export function BootScreen({ subsystems, onRetry }: BootScreenProps) {
  const total = subsystems.length;
  const ready = subsystems.filter((s) => s.state === 'ready').length;
  const pct = total === 0 ? 0 : Math.round((ready / total) * 100);
  const active = subsystems.find((s) => s.state === 'starting');
  const statusLine = active ? `Bringing ${active.label} online` : 'Bringing your agent online';

  return (
    <div className="relative flex h-full w-full items-center justify-center overflow-hidden bg-page-alt text-primary">
      <div className="pointer-events-none absolute inset-0 opacity-40">
        <RegressionPlot progress={total === 0 ? 0 : ready / total} seed={1337} count={24} />
      </div>
      <div
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            'radial-gradient(700px 500px at 50% 18%, rgba(106,166,255,.10), transparent 60%)',
        }}
      />

      <div className="relative z-10 flex flex-col items-center">
        <LabmateMark size={46} variant="tile" breathe spin="slow" />
        <h1 className="mt-4 text-lg font-semibold">Starting Labmate</h1>

        <div className="mt-6 w-[452px] rounded-card border border-border-1 bg-panel p-5 shadow-card">
          <div className="mb-1">
            {subsystems.map((s) => (
              <SubsystemRow key={s.id} s={s} onRetry={onRetry} />
            ))}
          </div>

          <progress
            data-testid="boot-progress"
            value={pct}
            max={100}
            aria-label={statusLine}
            className="mt-4 block h-1.5 w-full appearance-none overflow-hidden rounded-full [&::-moz-progress-bar]:[background:var(--brand-grad)] [&::-webkit-progress-bar]:rounded-full [&::-webkit-progress-bar]:bg-border-1 [&::-webkit-progress-value]:rounded-full [&::-webkit-progress-value]:transition-all [&::-webkit-progress-value]:duration-500 [&::-webkit-progress-value]:[background:var(--brand-grad)]"
          />
          <div className="mt-3 font-mono text-[11px] text-mono">{statusLine}</div>
        </div>
      </div>
    </div>
  );
}
