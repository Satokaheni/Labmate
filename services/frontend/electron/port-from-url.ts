// Extract an EXPLICIT :port from a ws(s):// gateway URL's authority. We parse the
// string directly rather than using WHATWG `URL.port`, because URL.port blanks a
// scheme-default port (`new URL('wss://host:443/ws').port === ''`) — so an explicit
// :443/:80 would be silently lost. Parsing the string keeps an explicit :443 as 443
// AND treats a NO-port URL as "unset" -> fall back to LOCAL_PORT, instead of binding
// 80/443 (which would EACCES on an ordinary machine).
const EXPLICIT_PORT = /^wss?:\/\/[^/:@]+:(\d+)(?:[/?#]|$)/i;

/** Parse the explicit port from a ws(s):// gateway URL. Falls back to LOCAL_PORT env, then 8787.
 *  A URL with no explicit port (e.g. `ws://localhost/ws`) yields the fallback, not 80/443. */
export function portFromWsUrl(wsUrl: string | null): number {
  if (wsUrl) {
    const m = wsUrl.match(EXPLICIT_PORT);
    if (m) return Number(m[1]);
  }
  return Number(process.env.LOCAL_PORT ?? 8787);
}
