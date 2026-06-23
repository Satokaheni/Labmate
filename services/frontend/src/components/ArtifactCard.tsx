import type { Artifact } from '@/types/events';
export function ArtifactCard({ artifact }: { artifact: Artifact; onPreview?: (a: Artifact) => void }) {
  return <div data-testid="artifact-card">{artifact.name}</div>;
}
