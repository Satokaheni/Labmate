import { LabmateMark } from '@/components/LabmateMark';
import { formatDuration } from '@/lib/format';
import type { NodeName, StreamEvent } from '@/types/events';

export interface ThinkingIndicatorProps {
  /** Events for the current turn, in order. */
  events: StreamEvent[];
  /** ms timestamp when the turn started. */
  startedAt: number;
  /** current ms timestamp (injectable for tests; default Date.now). */
  now?: number;
}

interface Phase {
  label: string;
  color: string;
}

interface CompletedStep {
  label: string;
  node: NodeName | string;
}

const PHASE_COLOR = {
  planning: '#6aa6ff',
  reasoning: '#a78bfa',
  tool: '#8c9bf0',
  writing: '#6aa6ff',
} as const;

/** Reduce the ordered event list into current phase, completed steps, and done state. */
function reduceEvents(events: StreamEvent[]): {
  phase: Phase | null;
  steps: CompletedStep[];
  done: boolean;
} {
  let phase: Phase | null = null;
  const steps: CompletedStep[] = [];
  let done = false;

  for (const e of events) {
    switch (e.type) {
      case 'node.enter':
        phase = { label: 'Planning', color: PHASE_COLOR.planning };
        break;
      case 'reasoning.delta':
        phase = { label: 'Reasoning', color: PHASE_COLOR.reasoning };
        break;
      case 'reasoning.done':
        steps.push({ label: e.reasoning.summary, node: e.reasoning.node });
        break;
      case 'tool.start':
        phase = { label: `Running ${e.toolCall.name}`, color: PHASE_COLOR.tool };
        break;
      case 'tool.done':
        steps.push({ label: e.summary || 'Tool finished', node: e.toolId });
        break;
      case 'answer.delta':
        phase = { label: 'Writing', color: PHASE_COLOR.writing };
        break;
      case 'turn.done':
        done = true;
        phase = null;
        break;
      default:
        break;
    }
  }
  return { phase, steps, done };
}

export function ThinkingIndicator({ events, startedAt, now = Date.now() }: ThinkingIndicatorProps) {
  const { phase, steps, done } = reduceEvents(events);
  const elapsedMs = Math.max(0, now - startedAt);

  if (done) {
    return (
      <div className="flex flex-col gap-1.5">
        <span
          data-testid="thought-pill"
          className="inline-flex w-fit items-center gap-1.5 border px-2.5 py-1 font-mono text-[11px]"
          style={{ color: '#7e8693', borderColor: '#232831', borderRadius: '7px' }}
        >
          Thought for {formatDuration(elapsedMs)}
        </span>
        {steps.length > 0 && (
          <div className="flex flex-col gap-1">
            {steps.map((s, i) => (
              <span
                key={i}
                data-testid="completed-step"
                className="inline-flex w-fit items-center gap-2 rounded-pill px-2 py-1 text-[11px]"
                style={{ background: '#6aa6ff22', color: '#7fb0ff' }}
              >
                <span aria-hidden>✓</span>
                <span>{s.label}</span>
                <span className="font-mono opacity-70">· {String(s.node)}</span>
              </span>
            ))}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-2.5">
        <LabmateMark size={30} variant="onDark" spin="fast" />
        {phase && (
          <span data-testid="phase-label" className="text-sm" style={{ color: phase.color }}>
            {phase.label}…
          </span>
        )}
        <span data-testid="elapsed-timer" className="font-mono text-xs" style={{ color: '#5e6671' }}>
          {formatDuration(elapsedMs)}
        </span>
      </div>

      {steps.length > 0 && (
        <>
          <div className="h-px w-full" style={{ background: '#20242c' }} />
          <div className="flex flex-col gap-1">
            {steps.map((s, i) => (
              <span
                key={i}
                data-testid="completed-step"
                className="inline-flex w-fit items-center gap-2 rounded-pill px-2 py-1 text-[11px]"
                style={{ background: '#6aa6ff22', color: '#7fb0ff' }}
              >
                <span aria-hidden>✓</span>
                <span>{s.label}</span>
                <span className="font-mono opacity-70">· {String(s.node)}</span>
              </span>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
