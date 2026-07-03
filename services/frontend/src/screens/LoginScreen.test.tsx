import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi } from 'vitest';
import { LoginScreen } from './LoginScreen';

describe('LoginScreen', () => {
  it('renders email and password inputs', () => {
    render(<LoginScreen onSubmit={vi.fn()} />);
    expect(screen.getByLabelText('Email')).toBeInTheDocument();
    expect(screen.getByLabelText('Password')).toBeInTheDocument();
  });

  it('disables submit when fields are empty', () => {
    render(<LoginScreen onSubmit={vi.fn()} />);
    expect(screen.getByRole('button', { name: /sign in/i })).toBeDisabled();
  });

  it('enables submit and calls onSubmit with credentials', async () => {
    const onSubmit = vi.fn();
    render(<LoginScreen onSubmit={onSubmit} />);
    await userEvent.type(screen.getByLabelText('Email'), 'a@b.com');
    await userEvent.type(screen.getByLabelText('Password'), 'secret');
    const btn = screen.getByRole('button', { name: /sign in/i });
    expect(btn).toBeEnabled();
    await userEvent.click(btn);
    expect(onSubmit).toHaveBeenCalledWith({ email: 'a@b.com', password: 'secret', remember: false });
  });

  it('shows spinner and Authenticating… and disables inputs when submitting', () => {
    render(<LoginScreen onSubmit={vi.fn()} submitting />);
    expect(screen.getByText(/authenticating/i)).toBeInTheDocument();
    expect(screen.getByLabelText('Email')).toBeDisabled();
    expect(screen.getByLabelText('Password')).toBeDisabled();
  });

  it('shows an error row when error prop is set', () => {
    render(<LoginScreen onSubmit={vi.fn()} error="invalid_credentials" />);
    expect(screen.getByRole('alert')).toHaveTextContent(/invalid/i);
  });

  it('renders the health status dot', () => {
    render(<LoginScreen onSubmit={vi.fn()} />);
    expect(screen.getByTestId('health-dot')).toBeInTheDocument();
    expect(screen.getByText(/local instance reachable/i)).toBeInTheDocument();
  });

  it('renders the decorative experiment graphic', () => {
    render(<LoginScreen onSubmit={vi.fn()} />);
    expect(screen.getByTestId('experiment-graphic')).toBeInTheDocument();
    expect(screen.getByText(/read the diff, draft the paper/i)).toBeInTheDocument();
    expect(screen.getByText(/fig\.1 — training run/i)).toBeInTheDocument();
  });

  it('toggles password visibility', async () => {
    render(<LoginScreen onSubmit={vi.fn()} />);
    const pw = screen.getByLabelText('Password') as HTMLInputElement;
    expect(pw.type).toBe('password');
    await userEvent.click(screen.getByRole('button', { name: /show password/i }));
    expect(pw.type).toBe('text');
  });
});
