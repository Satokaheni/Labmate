export interface ExecuteToolResponse {
  result?: unknown;
  error?: string;
}

export interface ToolDescriptor {
  name: string;
  source: 'builtin' | 'mcp' | 'skill';
  namespace?: string;
  schema?: unknown;
}

declare global {
  interface Window {
    electronAPI?: {
      config: { wsUrl: string | null; isDev: boolean };
      token: string | null;
      setConfig(wsUrl: string): Promise<void>;
      setToken(token: string, remember: boolean): Promise<void>;
      clearToken(): Promise<void>;
      executeTool(name: string, args: Record<string, unknown>, sessionId?: string | null): Promise<ExecuteToolResponse>;
      getMcpTools(): Promise<ToolDescriptor[]>;
    };
  }
}

export {};
