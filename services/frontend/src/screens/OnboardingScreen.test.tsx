import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import { OnboardingScreen } from './OnboardingScreen';

describe('OnboardingScreen', () => {
  it('captures gateway + model URL and saves both', async () => {
    const setConfig = vi.fn().mockResolvedValue(undefined);
    (globalThis as any).window.electronAPI = { setConfig };

    render(<OnboardingScreen onSaved={() => {}} />);

    fireEvent.change(screen.getByLabelText(/gateway/i), {
      target: { value: 'ws://localhost:8787/ws' },
    });
    fireEvent.change(screen.getByLabelText(/model/i), {
      target: { value: 'https://pod-8000.proxy.runpod.net/v1' },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));

    expect(setConfig).toHaveBeenCalledWith({
      wsUrl: 'ws://localhost:8787/ws',
      gemmaBase: 'https://pod-8000.proxy.runpod.net/v1',
    });
  });
});
