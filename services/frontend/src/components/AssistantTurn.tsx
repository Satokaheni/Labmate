import type { Turn, Artifact } from '@/types/events';
import { LabmateMark } from './LabmateMark';
import { Markdown } from '@/lib/markdown';
import { ToolCallRow } from './ToolCallRow';
import { ArtifactCard } from './ArtifactCard';
import { ReasoningBlock } from './ReasoningBlock';

export interface AssistantTurnProps {
  turn: Turn;
  hideToolCalls?: boolean;
  onPreviewArtifact?: (artifact: Artifact) => void;
}

export function AssistantTurn({ turn, hideToolCalls, onPreviewArtifact }: AssistantTurnProps) {
  const hasToolCalls = turn.toolCalls && turn.toolCalls.length > 0;
  const toolCallCount = turn.toolCalls?.length ?? 0;

  return (
    <div className="space-y-2">
      {/* Avatar and text */}
      <div className="flex gap-3">
        <div className="pt-1">
          <LabmateMark size={32} variant="tile" spin="none" />
        </div>
        <div className="flex-1 min-w-0">
          {turn.text && <Markdown text={turn.text} />}
        </div>
      </div>

      {/* Reasoning block */}
      {turn.reasoning && <ReasoningBlock reasoning={turn.reasoning} />}

      {/* Tool calls section */}
      {hasToolCalls && (
        <div className="space-y-2">
          {hideToolCalls ? (
            <div data-testid="tool-calls-summary" className="text-xs text-secondary">
              {toolCallCount === 1 ? '1 tool call → panel' : `${toolCallCount} tool calls → panel`}
            </div>
          ) : (
            <div className="space-y-2">
              {turn.toolCalls!.map((toolCall) => (
                <ToolCallRow key={toolCall.id} toolCall={toolCall} />
              ))}
            </div>
          )}
        </div>
      )}

      {/* Artifacts */}
      {turn.artifacts && turn.artifacts.length > 0 && (
        <div className="space-y-2">
          {turn.artifacts.map((artifact) => (
            <ArtifactCard
              key={artifact.id}
              artifact={artifact}
              onPreview={onPreviewArtifact}
            />
          ))}
        </div>
      )}
    </div>
  );
}
