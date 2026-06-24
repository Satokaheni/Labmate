import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { FilePreview } from './FilePreview';
import type { Artifact } from '@/types/events';

const code: Artifact = {
  id: 'f1', name: 'main.py', path: '/work/main.py', language: 'python', mime: 'text/x-python',
  sizeBytes: 20, lineCount: 2, preview: 'code', content: 'a = 1\nb = 2', downloadUrl: '/dl/f1',
};

describe('FilePreview', () => {
  it('renders an empty state when no artifact is selected', () => {
    render(<FilePreview artifact={null} />);
    expect(screen.getByText(/no file selected/i)).toBeInTheDocument();
  });

  it('renders line numbers for code preview', () => {
    render(<FilePreview artifact={code} />);
    expect(screen.getByText('a = 1')).toBeInTheDocument();
    expect(screen.getAllByTestId('line-number')).toHaveLength(2);
  });
});
