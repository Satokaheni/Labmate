import { describe, it, expect } from 'vitest';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { registerAllTools } from '../../src/registry.js';

describe('registerAllTools', () => {
  it('registers all tools without throwing', () => {
    const server = new McpServer({ name: 'test', version: '0.0.1' });
    expect(() => registerAllTools(server)).not.toThrow();
  });
});
