import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { Markdown } from './markdown';

describe('Markdown', () => {
  it('renders bold text', () => {
    const { container } = render(<Markdown text="hello **world**" />);
    expect(container.querySelector('strong')).toHaveTextContent('world');
  });

  it('renders inline code', () => {
    const { container } = render(<Markdown text="run `npm test` now" />);
    expect(container.querySelector('code')).toHaveTextContent('npm test');
  });

  it('renders fenced code blocks', () => {
    render(<Markdown text={'```\nconst x = 1\n```'} />);
    expect(screen.getByText('const x = 1')).toBeInTheDocument();
  });

  it('does not render raw HTML (xss safe)', () => {
    const { container } = render(<Markdown text={'<img src=x onerror=alert(1)>'} />);
    expect(container.querySelector('img')).not.toBeInTheDocument();
  });
});
