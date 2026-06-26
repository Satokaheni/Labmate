import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect } from 'vitest';
import { App } from './App';
import type { Artifact, Turn } from '@/types/events';

const createArtifact = (overrides?: Partial<Artifact>): Artifact => ({
  id: 'artifact-1',
  name: 'example.txt',
  path: '/path/to/example.txt',
  language: 'plaintext',
  mime: 'text/plain',
  sizeBytes: 1024,
  lineCount: 42,
  preview: 'code',
  content: 'Hello, world!',
  downloadUrl: 'https://example.com/download/artifact-1',
  ...overrides,
});

const createAssistantTurn = (overrides?: Partial<Turn>): Turn => ({
  id: 'turn-1',
  sessionId: 'session-1',
  role: 'assistant',
  text: 'I created a file for you.',
  createdAt: '2026-06-25T00:00:00Z',
  artifacts: [createArtifact()],
  status: 'complete',
  ...overrides,
});

describe('App', () => {
  it('renders left and center columns; no right panel when no artifact', () => {
    render(<App />);
    expect(screen.getByTestId('layout-left')).toBeInTheDocument();
    expect(screen.getByTestId('layout-center')).toBeInTheDocument();
    expect(screen.queryByTestId('layout-right')).not.toBeInTheDocument();
  });

  it('right panel appears with live trace when debug on', async () => {
    render(<App />);
    expect(screen.queryByTestId('layout-right')).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /debug/i }));
    expect(screen.getByTestId('layout-right')).toHaveTextContent(/live trace/i);
  });

  it('file preview button is not rendered when no artifact is previewed', () => {
    render(<App turns={[createAssistantTurn()]} />);
    expect(screen.queryByTestId('file-preview-btn')).not.toBeInTheDocument();
  });

  it('clicking an artifact preview button shows the file preview button and opens the FilePreview panel', async () => {
    const artifact = createArtifact({ name: 'test.md', preview: 'doc' });
    const turn = createAssistantTurn({ artifacts: [artifact] });

    render(<App turns={[turn]} />);

    // Initially, file preview button should not be visible
    expect(screen.queryByTestId('file-preview-btn')).not.toBeInTheDocument();
    expect(screen.queryByTestId('layout-right')).not.toBeInTheDocument();

    // Click the artifact card's Preview button
    const previewButton = screen.getByRole('button', { name: /preview/i });
    await userEvent.click(previewButton);

    // Now the file preview button should be visible in the topBar
    const filePreviewBtn = screen.getByTestId('file-preview-btn');
    expect(filePreviewBtn).toBeInTheDocument();
    expect(filePreviewBtn).toHaveAttribute('aria-label', `Toggle file preview: ${artifact.name}`);

    // And the right panel should show FilePreview content
    expect(screen.getByTestId('layout-right')).toBeInTheDocument();
  });

  it('file preview button toggles the panel off/on (aria-pressed changes)', async () => {
    const artifact = createArtifact({ name: 'example.py' });
    const turn = createAssistantTurn({ artifacts: [artifact] });

    render(<App turns={[turn]} />);

    // Click the artifact preview button to show it
    await userEvent.click(screen.getByRole('button', { name: /preview/i }));

    const filePreviewBtn = screen.getByTestId('file-preview-btn');

    // Initially it should be pressed (aria-pressed=true)
    expect(filePreviewBtn).toHaveAttribute('aria-pressed', 'true');

    // Click to toggle off
    await userEvent.click(filePreviewBtn);
    expect(filePreviewBtn).toHaveAttribute('aria-pressed', 'false');
    expect(screen.queryByTestId('layout-right')).not.toBeInTheDocument();

    // Click to toggle on
    await userEvent.click(filePreviewBtn);
    expect(filePreviewBtn).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByTestId('layout-right')).toBeInTheDocument();
  });
});
