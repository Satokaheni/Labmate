export type { ToolSource, ToolDescriptor, ClientCapabilities } from './contract.generated.js';
export { BUILTIN_TOOL_NAMES } from './contract.generated.js';
import type { ClientCapabilities, ToolDescriptor } from './contract.generated.js';
import { BUILTIN_TOOL_NAMES } from './contract.generated.js';

// The capabilities this client can currently execute (five builtins).
export const CLIENT_CAPABILITIES: ClientCapabilities = {
  protocolVersion: 1,
  tools: BUILTIN_TOOL_NAMES.map((name) => ({ name, source: 'builtin' as const })),
};

export function capabilitiesFrame(extraTools: ToolDescriptor[] = []): { type: 'client.capabilities' } & ClientCapabilities {
  return {
    type: 'client.capabilities',
    protocolVersion: CLIENT_CAPABILITIES.protocolVersion,
    tools: [...CLIENT_CAPABILITIES.tools, ...extraTools],
  };
}
