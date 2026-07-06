import * as fs from 'node:fs';
import * as path from 'node:path';

// Default gateway WS URL for a LOCAL single-process harness (services.local.main
// on LOCAL_PORT 8787). Kept in sync with src/gateway-defaults.ts's DEFAULT_WS_URL;
// duplicated here (rather than imported) because electron/tsconfig.json's rootDir
// is scoped to electron/ so its compiled output stays flat under dist-electron/.
const DEFAULT_WS_URL = 'ws://localhost:8787/ws';

export interface AppConfig {
  wsUrl: string | null;
  gemmaBase: string | null;
  isDev: boolean;
}

function configPath(userData: string): string {
  return path.join(userData, 'config.json');
}

export function loadConfig(userData: string): AppConfig {
  const isDev = process.env.ELECTRON_DEV === '1';
  try {
    const raw = JSON.parse(fs.readFileSync(configPath(userData), 'utf8'));
    return {
      wsUrl: typeof raw.wsUrl === 'string' ? raw.wsUrl : null,
      gemmaBase: typeof raw.gemmaBase === 'string' ? raw.gemmaBase : null,
      isDev,
    };
  } catch {
    // No config yet: dev falls back to the default WS URL; endpoint stays unset.
    return { wsUrl: isDev ? DEFAULT_WS_URL : null, gemmaBase: null, isDev };
  }
}

export function saveConfig(
  cfg: { wsUrl: string | null; gemmaBase: string | null },
  userData: string,
): void {
  fs.writeFileSync(
    configPath(userData),
    JSON.stringify({ wsUrl: cfg.wsUrl, gemmaBase: cfg.gemmaBase }, null, 2),
    'utf8',
  );
}
