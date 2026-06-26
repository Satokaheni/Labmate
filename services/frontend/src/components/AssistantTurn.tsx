import { LabmateMark } from '@/components/LabmateMark';
import { Markdown } from '@/lib/markdown';
import { ReasoningBlock } from './ReasoningBlock';
import { ToolCallRow } from './ToolCallRow';
import { ArtifactCard } from './ArtifactCard';
import type { Artifact, Turn as TurnT } from '@/types/events';

export interface AssistantTurnProps {
  turn: TurnT;
  onPreviewArtifact?: (artifact: Artifact) => void;
  hideToolCalls?: boolean;
}

export function AssistantTurn({ turn, onPreviewArtifact, hideToolCalls }: AssistantTurnProps) {
  const isStreaming = turn.status === 'streaming';
  const isEmpty = !turn.text && !turn.reasoning && (!turn.toolCalls || turn.toolCalls.length === 0);
  const toolCount = turn.toolCalls?.length ?? 0;

  return (
    <div data-testid="assistant-turn" className="flex gap-3 py-3">
      <div className="mt-0.5 shrink-0">
        <LabmateMark size={18} variant="tile" spin={isStreaming && isEmpty ? 'fast' : 'none'} />
      </div>
      <div className="min-w-0 flex-1">
        {turn.reasoning && <ReasoningBlock reasoning={turn.reasoning} />}

        {toolCount > 0 && (
          hideToolCalls ? (
            <div
              data-testid="tool-calls-summary"
              className="mb-2 font-mono text-[11px] text-mono"
            >
              {toolCount} tool call{toolCount !== 1 ? 's' : ''} → panel
            </div>
          ) : (
            <div className="mb-2 flex flex-col gap-1">
              {turn.toolCalls!.map((tc) => (
                <ToolCallRow key={tc.id} toolCall={tc} />
              ))}
            </div>
          )
        )}

        {turn.text && <Markdown text={turn.text} />}

        {turn.artifacts && turn.artifacts.length > 0 && (
          <div className="mt-3 flex flex-col gap-2">
            {turn.artifacts.map((a) => (
              <ArtifactCard key={a.id} artifact={a} onPreview={onPreviewArtifact} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
