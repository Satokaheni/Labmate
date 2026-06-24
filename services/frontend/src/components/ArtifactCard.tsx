import type { Artifact } from '@/types/events';
import { formatBytes } from '@/lib/format';

export interface ArtifactCardProps {
  artifact: Artifact;
  onPreview?: (artifact: Artifact) => void;
}

export function ArtifactCard({ artifact, onPreview }: ArtifactCardProps) {
  return (
    <div
      data-testid="artifact-card"
      className="flex items-center gap-3 rounded-pill border border-border-3 bg-panel px-3 py-2.5"
    >
      <span className="grid h-8 w-8 place-items-center rounded bg-page font-mono text-xs text-mono">
        {artifact.preview === 'doc' ? '📄' : '{}'}
      </span>
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm text-primary">{artifact.name}</div>
        <div className="font-mono text-[11px] text-mono">
          <span>{artifact.language}</span>
          {' · '}
          <span>{formatBytes(artifact.sizeBytes)}</span>
          {artifact.lineCount != null ? <><span>{' · '}</span><span>{artifact.lineCount} lines</span></> : null}
        </div>
      </div>
      <button
        type="button"
        onClick={() => onPreview?.(artifact)}
        className="rounded-pill border border-border-3 px-2.5 py-1 text-xs text-secondary hover:text-primary"
      >
        Preview
      </button>
      <a
        href={artifact.downloadUrl}
        download
        rel="noopener noreferrer"
        className="rounded-pill border border-border-3 px-2.5 py-1 text-xs text-secondary hover:text-primary"
      >
        Download
      </a>
    </div>
  );
}
