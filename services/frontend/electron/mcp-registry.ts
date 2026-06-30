import { McpHost, type McpServerSpec } from './mcp-host.js';
import fs from 'node:fs';
import path from 'node:path';

/**
 * Tool descriptor matching the frontend's ToolDescriptor type from capabilities.
 * source: 'builtin' | 'mcp' | 'skill'
 * schema is an OpenAI tool object (required for mcp/skill sources)
 */
export interface ToolDescriptor {
  name: string;
  source: 'builtin' | 'mcp' | 'skill';
  namespace?: string;
  schema?: unknown;
}

/**
 * Resolve the skills directory.
 * Uses LABMATE_SKILLS_DIR env var if set, else computes relative path from repo root.
 * Frontend is at <repo>/services/frontend, skills at <repo>/services/skills.
 */
export function skillsDir(): string {
  if (process.env.LABMATE_SKILLS_DIR) {
    return process.env.LABMATE_SKILLS_DIR;
  }
  // Frontend is at services/frontend; go up two levels to repo root, then into skills
  const repoRoot = path.resolve(__dirname, '../../..');
  return path.join(repoRoot, 'services', 'skills');
}

/**
 * Built-in TS skill MCP servers.
 * Only the three TS skills; if dist/index.js doesn't exist, they'll be skipped.
 */
const BUILTIN_MCP_SERVERS = ['ast-ts-refactor', 'component-doc-gen', 'a11y-audit'];

/**
 * McpHostManager hosts bundled TS-skill MCP servers and collects their tools
 * as namespaced capability-manifest descriptors.
 */
export class McpHostManager {
  private hosts: Map<string, McpHost> = new Map();
  private toolDescriptors: ToolDescriptor[] = [];

  /**
   * Start all available MCP servers.
   * Servers whose dist/index.js doesn't exist are skipped (logged).
   * If a server fails to start, it's skipped and others continue.
   */
  async startAll(): Promise<void> {
    const skillsDirPath = skillsDir();

    for (const skillName of BUILTIN_MCP_SERVERS) {
      const distPath = path.join(skillsDirPath, skillName, 'dist', 'index.js');

      // Check if dist exists; skip if not
      if (!fs.existsSync(distPath)) {
        console.error(`Skipping MCP server '${skillName}': ${distPath} does not exist`);
        continue;
      }

      const spec: McpServerSpec = {
        name: skillName,
        command: 'node',
        args: [distPath],
      };

      const host = new McpHost(spec);

      try {
        await host.start();
        this.hosts.set(skillName, host);

        // Collect tools from this host
        const tools = await host.listTools();
        for (const tool of tools) {
          const descriptor: ToolDescriptor = {
            name: tool.name,
            source: 'mcp',
            namespace: skillName,
            schema: {
              type: 'function',
              function: {
                name: tool.name,
                description: tool.description ?? '',
                parameters: tool.inputSchema ?? { type: 'object', properties: {} },
              },
            },
          };
          this.toolDescriptors.push(descriptor);
        }
      } catch (error) {
        console.error(
          `Failed to start MCP server '${skillName}': ${error instanceof Error ? error.message : String(error)}`
        );
        // Continue with next server; one failure doesn't abort others
      }
    }
  }

  /**
   * Get all collected tool descriptors from started servers.
   * This is sync (cached during startAll).
   */
  getToolDescriptors(): ToolDescriptor[] {
    return this.toolDescriptors;
  }

  /**
   * Call a tool on the appropriate MCP server.
   * namespacedName format: "mcp__<server>__<tool>"
   * Throws if the namespace or tool is unknown.
   */
  async callTool(namespacedName: string, args: Record<string, unknown>): Promise<unknown> {
    // Parse "mcp__<server>__<tool>"
    const match = namespacedName.match(/^mcp__([^_]+(?:_[^_]+)*)__(.+)$/);
    if (!match) {
      throw new Error(
        `Invalid namespaced tool name: ${namespacedName}. Expected format: mcp__<server>__<tool>`
      );
    }

    const [, serverName, toolName] = match;
    const host = this.hosts.get(serverName);
    if (!host) {
      throw new Error(`Unknown MCP server: ${serverName}`);
    }

    try {
      return await host.callTool(toolName, args);
    } catch (error) {
      throw new Error(
        `Failed to call tool '${toolName}' on server '${serverName}': ${error instanceof Error ? error.message : String(error)}`
      );
    }
  }

  /**
   * Stop all started MCP servers.
   * Idempotent; never throws.
   */
  async stopAll(): Promise<void> {
    const errors: string[] = [];

    for (const [name, host] of this.hosts.entries()) {
      try {
        await host.stop();
      } catch (error) {
        errors.push(
          `Failed to stop '${name}': ${error instanceof Error ? error.message : String(error)}`
        );
      }
    }

    this.hosts.clear();

    // Log errors but don't throw
    if (errors.length > 0) {
      console.error('Errors during stopAll:', errors.join('; '));
    }
  }
}
