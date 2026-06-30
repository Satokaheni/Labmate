import os from 'node:os';
import fs from 'node:fs';
import path from 'node:path';
import type { McpServerSpec } from './mcp-host.js';

/** Global Labmate home, like ~/.claude. Override with LABMATE_HOME (tests/dev). */
export function labmateHome(): string {
  return process.env.LABMATE_HOME && process.env.LABMATE_HOME.length > 0
    ? process.env.LABMATE_HOME
    : path.join(os.homedir(), '.labmate');
}

/** Global user skills dir: <labmateHome>/skills. Each skill is a subfolder with a SKILL.md. */
export function userSkillsDir(): string {
  return path.join(labmateHome(), 'skills');
}

/** Best-effort: ensure the user skills dir exists so the user can drop skill folders in.
 *  Never throws (a failure must not break app startup). Returns the path. */
export function ensureUserSkillsDir(): string {
  const dir = userSkillsDir();
  try {
    fs.mkdirSync(dir, { recursive: true });
  } catch (err) {
    console.error('failed to ensure user skills dir:', err);
  }
  return dir;
}

/** Path to the user MCP servers config file: <labmateHome>/mcp.json */
export function labmateMcpConfigPath(): string {
  return path.join(labmateHome(), 'mcp.json');
}

/**
 * Read and parse user-installed MCP servers from ~/.labmate/mcp.json.
 * Never throws; tolerates: file absent → []; JSON parse error → [] (logged);
 * mcpServers missing/not-an-object → []; entries missing command → skipped (logged).
 * Returns an array of McpServerSpec with name/command/args/cwd?/env?.
 */
export function readUserMcpServers(): McpServerSpec[] {
  const configPath = labmateMcpConfigPath();

  // Tolerate missing file
  if (!fs.existsSync(configPath)) {
    return [];
  }

  try {
    const content = fs.readFileSync(configPath, 'utf-8');
    const config = JSON.parse(content);

    // Tolerate missing or non-object mcpServers
    if (!config.mcpServers || typeof config.mcpServers !== 'object') {
      return [];
    }

    const servers: McpServerSpec[] = [];

    for (const [name, spec] of Object.entries(config.mcpServers)) {
      // Skip entries where spec is not an object
      if (!spec || typeof spec !== 'object') {
        continue;
      }

      const serverSpec = spec as Record<string, unknown>;

      // Skip entries missing a non-empty command
      if (!serverSpec.command || typeof serverSpec.command !== 'string' || !serverSpec.command.trim()) {
        console.error(`Skipping MCP server '${name}': missing or empty 'command' field`);
        continue;
      }

      // Skip entries where name contains __ (reserved for namespacing)
      if (name.includes('__')) {
        console.error(
          `Skipping MCP server '${name}': name contains '__' which is reserved for tool namespacing`
        );
        continue;
      }

      // Extract args (default to [])
      const args = Array.isArray(serverSpec.args) ? (serverSpec.args as string[]) : [];

      // Extract optional cwd and env
      const cwd = serverSpec.cwd && typeof serverSpec.cwd === 'string' ? serverSpec.cwd : undefined;
      const env =
        serverSpec.env && typeof serverSpec.env === 'object'
          ? (serverSpec.env as Record<string, string>)
          : undefined;

      servers.push({
        name,
        command: serverSpec.command,
        args,
        ...(cwd && { cwd }),
        ...(env && { env }),
      });
    }

    return servers;
  } catch (err) {
    console.error(`Failed to read/parse MCP config from ${configPath}:`, err);
    return [];
  }
}
