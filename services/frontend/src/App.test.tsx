import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect } from 'vitest';
import { App } from './App';

describe('App', () => {
  it('renders all three columns', () => {
    render(<App />);
    expect(screen.getByTestId('layout-left')).toBeInTheDocument();
    expect(screen.getByTestId('layout-center')).toBeInTheDocument();
    expect(screen.getByTestId('layout-right')).toBeInTheDocument();
  });

  it('right panel shows file preview by default and live trace when debug on', async () => {
    render(<App />);
    expect(screen.getByTestId('layout-right')).toHaveTextContent(/no file selected/i);
    await userEvent.click(screen.getByRole('button', { name: /debug/i }));
    expect(screen.getByTestId('layout-right')).toHaveTextContent(/live trace/i);
  });
});
