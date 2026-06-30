export type ToolSource = 'builtin' | 'mcp' | 'skill';

export interface ToolDescriptor {
  name: string;
  source: ToolSource;
  namespace?: string;
  schema?: unknown; // OpenAI tool object; required for mcp/skill (Phase 2), omitted for builtins
}

export interface ClientCapabilities {
  protocolVersion: number;
  tools: ToolDescriptor[];
}

// The capabilities this client can currently execute (Phase 0: the four builtins).
export const CLIENT_CAPABILITIES: ClientCapabilities = {
  protocolVersion: 1,
  tools: [
    { name: 'read_file', source: 'builtin' },
    { name: 'write_file', source: 'builtin' },
    { name: 'list_dir', source: 'builtin' },
    { name: 'search_files', source: 'builtin' },
  ],
};

export function capabilitiesFrame(): { type: 'client.capabilities' } & ClientCapabilities {
  return { type: 'client.capabilities', ...CLIENT_CAPABILITIES };
}
