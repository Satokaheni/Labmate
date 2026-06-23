import { Markdown } from '@/lib/markdown';
import { AssistantTurn } from './AssistantTurn';
import type { Artifact, Turn as TurnT } from '@/types/events';

export interface TurnProps {
  turn: TurnT;
  onPreviewArtifact?: (artifact: Artifact) => void;
}

export function Turn({ turn, onPreviewArtifact }: TurnProps) {
  if (turn.role === 'user') {
    return (
      <div data-testid="user-turn" className="flex justify-end py-3">
        <div className="max-w-[75%] rounded-card rounded-tr-sm border border-border-2 bg-panel px-3.5 py-2">
          <Markdown text={turn.text} />
        </div>
      </div>
    );
  }
  return <AssistantTurn turn={turn} onPreviewArtifact={onPreviewArtifact} />;
}
