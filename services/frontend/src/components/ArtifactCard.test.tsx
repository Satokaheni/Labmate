import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi } from 'vitest';
import { ArtifactCard } from './ArtifactCard';
import type { Artifact } from '@/types/events';

const artifact: Artifact = {
  id: 'f1', name: 'main.py', path: '/work/main.py', language: 'python', mime: 'text/x-python',
  sizeBytes: 2048, lineCount: 40, preview: 'code', content: 'print("hi")', downloadUrl: '/dl/f1',
};

describe('ArtifactCard', () => {
  it('shows filename, language and size', () => {
    render(<ArtifactCard artifact={artifact} />);
    expect(screen.getByText('main.py')).toBeInTheDocument();
    expect(screen.getByText(/python/i)).toBeInTheDocument();
    expect(screen.getByText('2.0 KB')).toBeInTheDocument();
  });

  it('fires onPreview with the artifact when Preview clicked', async () => {
    const onPreview = vi.fn();
    render(<ArtifactCard artifact={artifact} onPreview={onPreview} />);
    await userEvent.click(screen.getByRole('button', { name: /preview/i }));
    expect(onPreview).toHaveBeenCalledWith(artifact);
  });
});
