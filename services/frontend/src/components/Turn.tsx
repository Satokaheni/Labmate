import type { Turn as TurnT, Artifact } from '@/types/events';
import { AssistantTurn } from './AssistantTurn';

export interface TurnProps {
  turn: TurnT;
  onPreviewArtifact?: (artifact: Artifact) => void;
}

export function Turn({ turn, onPreviewArtifact }: TurnProps) {
  if (turn.role === 'user') {
    return (
      <div data-testid="user-turn" className="flex justify-end">
        <div className="max-w-2xl text-right">{turn.text}</div>
      </div>
    );
  }

  // For assistant (and any other role), delegate to AssistantTurn
  return <AssistantTurn turn={turn} onPreviewArtifact={onPreviewArtifact} />;
}
