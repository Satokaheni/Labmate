import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { ChatLayout } from './ChatLayout';

describe('ChatLayout', () => {
  it('renders the three columns and top bar slots', () => {
    render(
      <ChatLayout
        topBar={<div>TOPBAR</div>}
        left={<div>LEFT</div>}
        center={<div>CENTER</div>}
        right={<div>RIGHT</div>}
      />
    );
    expect(screen.getByTestId('layout-topbar')).toHaveTextContent('TOPBAR');
    expect(screen.getByTestId('layout-left')).toHaveTextContent('LEFT');
    expect(screen.getByTestId('layout-center')).toHaveTextContent('CENTER');
    expect(screen.getByTestId('layout-right')).toHaveTextContent('RIGHT');
  });
});
