// WHATWG URL blanks out `.port` when it equals the scheme's default port
// (e.g. `new URL('wss://host:443/ws').port === ''`), so an explicit 443/80
// in a ws(s):// URL would otherwise be silently lost. Recover it here.
const DEFAULT_PORTS: Record<string, number> = { 'ws:': 80, 'wss:': 443 };

/** Parse the port from a ws(s):// gateway URL. Falls back to LOCAL_PORT env, then 8787. */
export function portFromWsUrl(wsUrl: string | null): number {
  if (wsUrl) {
    try {
      const u = new URL(wsUrl);
      if (u.port) return Number(u.port);
      const defaultPort = DEFAULT_PORTS[u.protocol];
      if (defaultPort !== undefined) return defaultPort;
    } catch {
      /* fall through */
    }
  }
  return Number(process.env.LOCAL_PORT ?? 8787);
}
