declare global {
  interface Window {
    electronAPI?: {
      config: { wsUrl: string | null; isDev: boolean };
      token: string | null;
      setConfig: (wsUrl: string) => Promise<void>;
      setToken: (token: string, remember: boolean) => Promise<void>;
      clearToken: () => Promise<void>;
      executeTool: (name: string, args: Record<string, unknown>) => Promise<{ result?: unknown; error?: string }>;
    };
  }
}

const ec = window.electronAPI?.config;

// In dev (ELECTRON_DEV=1) or browser, use the Vite env var.
// In a packaged build, use the runtime URL from userData/config.json.
export const WS_URL: string = ec?.isDev
  ? (import.meta.env.VITE_WS_URL as string | undefined) ?? 'ws://localhost:8787/ws'
  : ec?.wsUrl ?? '';

export const API_URL: string = WS_URL
  .replace(/^wss:\/\//, 'https://')
  .replace(/^ws:\/\//, 'http://')
  .replace(/\/ws$/, '');
