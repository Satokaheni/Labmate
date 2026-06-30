export type ToolSource = 'builtin' | 'mcp' | 'skill';

export interface ToolDescriptor {
  name: string;
  source: ToolSource;
  namespace?: string;
  schema?: unknown; // OpenAI tool object; required for mcp/skill (Phase 2), omitted for builtins
  description?: string; // Tool description (routing signal for skills)
  body?: string; // Skill instructions/body (for skills only)
}

export interface ClientCapabilities {
  protocolVersion: number;
  tools: ToolDescriptor[];
}

// The capabilities this client can currently execute (Phase 1: five builtins).
export const CLIENT_CAPABILITIES: ClientCapabilities = {
  protocolVersion: 1,
  tools: [
    { name: 'read_file', source: 'builtin' },
    { name: 'write_file', source: 'builtin' },
    { name: 'list_dir', source: 'builtin' },
    { name: 'search_files', source: 'builtin' },
    { name: 'run_tests', source: 'builtin' },
  ],
};

export function capabilitiesFrame(extraTools: ToolDescriptor[] = []): { type: 'client.capabilities' } & ClientCapabilities {
  return {
    type: 'client.capabilities',
    protocolVersion: CLIENT_CAPABILITIES.protocolVersion,
    tools: [...CLIENT_CAPABILITIES.tools, ...extraTools],
  };
}
