import { Markdown } from '@/lib/markdown';
import { formatBytes } from '@/lib/format';
import type { Artifact } from '@/types/events';

export function FilePreview({ artifact }: { artifact: Artifact | null }) {
  if (!artifact) {
    return (
      <div className="flex h-full items-center justify-center p-6 text-center text-sm text-mono">
        No file selected
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-border-1 px-4 py-3">
        <div className="min-w-0">
          <div className="truncate text-sm text-primary">{artifact.name}</div>
          <div className="font-mono text-[11px] text-mono">
            {artifact.language} · {formatBytes(artifact.sizeBytes)}
          </div>
        </div>
        <a href={artifact.downloadUrl} download rel="noopener noreferrer" className="text-xs text-secondary hover:text-primary">
          Download
        </a>
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        {artifact.preview === 'doc' ? (
          <div className="p-4">
            <Markdown text={artifact.content} />
          </div>
        ) : (
          <pre className="p-3 font-mono text-xs leading-relaxed">
            {artifact.content.split('\n').map((line, i) => (
              <div key={`${i + 1}:${line.slice(0, 16)}`} className="flex">
                <span
                  data-testid="line-number"
                  className="mr-3 inline-block w-8 shrink-0 select-none text-right text-mono"
                >
                  {i + 1}
                </span>
                <span className="text-primary-alt">{line}</span>
              </div>
            ))}
          </pre>
        )}
      </div>
    </div>
  );
}
