import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect } from 'vitest';
import { App } from './App';

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
});
