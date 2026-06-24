export interface ExecuteToolResponse {
  result?: unknown;
  error?: string;
}

declare global {
  interface Window {
    electronAPI?: {
      executeTool(name: string, args: Record<string, unknown>): Promise<ExecuteToolResponse>;
    };
  }
}

export {};
