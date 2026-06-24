export const WS_URL: string =
  (import.meta.env.VITE_WS_URL as string | undefined) ?? 'ws://localhost:8787/ws';

export const API_URL: string = WS_URL.replace(/^ws/, 'http').replace(/\/ws$/, '');
