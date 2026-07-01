// DO NOT EDIT — generated from contract/manifest.contract.json by scripts/gen_contract.py
export type ToolSource = 'builtin' | 'mcp' | 'skill';

export interface ToolDescriptor {
  name: string;
  source: ToolSource;
  namespace?: string;
  schema?: unknown;
  description?: string;
  body?: string;
}

export interface ClientCapabilities {
  protocolVersion?: number;
  tools: ToolDescriptor[];
}

export const BUILTIN_TOOL_NAMES = ['read_file', 'write_file', 'list_dir', 'search_files', 'run_tests'] as const;
