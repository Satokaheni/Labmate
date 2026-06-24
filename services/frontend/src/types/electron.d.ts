export interface ExecuteToolResponse {
  result?: unknown;
  error?: string;
}

declare global {
  interface Window {
    electronAPI?: {
      config: { wsUrl: string | null; isDev: boolean };
      token: string | null;
      setConfig(wsUrl: string): Promise<void>;
      setToken(token: string, remember: boolean): Promise<void>;
      clearToken(): Promise<void>;
      executeTool(name: string, args: Record<string, unknown>): Promise<ExecuteToolResponse>;
    };
  }
}

export {};
