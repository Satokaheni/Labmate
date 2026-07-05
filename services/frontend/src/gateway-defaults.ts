/** Default gateway WS URL for a LOCAL single-process harness (services.local.main
 *  on LOCAL_PORT 8787). Override per-machine via VITE_WS_URL (.env, dev) or the
 *  saved userData/config.json (packaged build). */
export const DEFAULT_WS_URL = 'ws://localhost:8787/ws';
