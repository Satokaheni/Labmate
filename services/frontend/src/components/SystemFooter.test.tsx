import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { SystemFooter } from './SystemFooter';
import type { AgentStatus } from '@/types/events';

const status: AgentStatus = {
  brain: { model: 'gemma-31b', endpoint: ':8000', state: 'active', node: 'plan_node', thinkingBudget: 2000 },
  nervousSystem: { name: 'MCP bridge', transport: 'stdio', state: 'connected', toolsRegistered: 12 },
  hands: { skills: [{ name: 'web_search', state: 'idle' }, { name: 'code_sandbox', state: 'active' }] },
};

describe('SystemFooter', () => {
  it('shows brain, MCP and hands one-liners', () => {
    render(<SystemFooter status={status} />);
    expect(screen.getByTestId('footer-brain')).toHaveTextContent('gemma-31b');
    expect(screen.getByTestId('footer-nervous')).toHaveTextContent(/12 tools/i);
    expect(screen.getByTestId('footer-hands')).toHaveTextContent(/2 skills/i);
  });
});
