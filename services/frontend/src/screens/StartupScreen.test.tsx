import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { StartupScreen } from './StartupScreen';

describe('StartupScreen', () => {
  it('shows the current step while starting', () => {
    render(<StartupScreen status={{ phase: 'starting', step: 'waiting for backend health' }} />);
    expect(screen.getByText(/waiting for backend health/i)).toBeInTheDocument();
  });

  it('shows the log tail + Retry on boot_failed', () => {
    render(<StartupScreen status={{ phase: 'boot_failed', logTail: 'Traceback: boom' }} />);
    expect(screen.getByText(/Traceback: boom/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('shows the model URL on model_unreachable', () => {
    render(<StartupScreen status={{ phase: 'model_unreachable', url: 'https://pod/v1' }} />);
    expect(screen.getByText(/https:\/\/pod\/v1/)).toBeInTheDocument();
  });
});
