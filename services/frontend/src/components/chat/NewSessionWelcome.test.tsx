import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { NewSessionWelcome } from './NewSessionWelcome';

describe('NewSessionWelcome', () => {
  it('renders the badge, greeting, subtext, composer slot, and 3 starter chips', () => {
    render(
      <NewSessionWelcome
        mode="code"
        greeting="Good evening"
        onStarter={() => {}}
        composer={<textarea data-testid="stub-composer" />}
      />,
    );
    expect(screen.getByTestId('orbit-mark')).toBeInTheDocument(); // from LabmateMark
    expect(screen.getByText('Good evening')).toBeInTheDocument();
    expect(screen.getByText(/pick up a milestone/)).toBeInTheDocument(); // code subtext
    expect(screen.getByTestId('stub-composer')).toBeInTheDocument();
    expect(screen.getByText('Scaffold a service')).toBeInTheDocument();
    expect(screen.getAllByTestId('starter-chip')).toHaveLength(3);
  });

  it('calls onStarter with the starter prompt when a chip is clicked', () => {
    const onStarter = vi.fn();
    render(
      <NewSessionWelcome mode="code" greeting="Good evening" onStarter={onStarter}
        composer={<textarea />} />,
    );
    fireEvent.click(screen.getByText('Map the repo'));
    expect(onStarter).toHaveBeenCalledWith('Map the repo');
  });
});
