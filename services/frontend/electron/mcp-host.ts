import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import type { Tool } from '@modelcontextprotocol/sdk/types.js';

export interface McpServerSpec {
  name: string;
  command: string;
  args: string[];
  cwd?: string;
}

/**
 * McpHost wraps a single MCP server connection via stdio transport.
 * Uses the official SDK Client + StdioClientTransport.
 */
export class McpHost {
  private spec: McpServerSpec;
  private client: Client | null = null;
  private transport: StdioClientTransport | null = null;

  constructor(spec: McpServerSpec) {
    this.spec = spec;
  }

  /**
   * Start the server and connect the MCP client.
   * Throws an Error if the server fails to start.
   */
  async start(): Promise<void> {
    if (this.client !== null) {
      throw new Error(`McpHost '${this.spec.name}' is already started`);
    }

    try {
      this.transport = new StdioClientTransport({
        command: this.spec.command,
        args: this.spec.args,
        cwd: this.spec.cwd,
      });

      this.client = new Client(
        {
          name: 'labmate-desktop',
          version: '0.1.0',
        },
        {
          capabilities: {},
        }
      );

      await this.client.connect(this.transport);
    } catch (error) {
      // Clean up on connection failure
      this.client = null;
      this.transport = null;
      throw new Error(
        `Failed to start MCP server '${this.spec.name}': ${error instanceof Error ? error.message : String(error)}`
      );
    }
  }

  /**
   * List all tools advertised by the server.
   * Returns an array of tool descriptors with name, description, and inputSchema.
   */
  async listTools(): Promise<Array<{ name: string; description?: string; inputSchema: unknown }>> {
    if (this.client === null) {
      throw new Error(`McpHost '${this.spec.name}' is not started`);
    }

    try {
      const response = await this.client.listTools();
      return response.tools.map((tool: Tool) => ({
        name: tool.name,
        description: tool.description,
        inputSchema: tool.inputSchema,
      }));
    } catch (error) {
      throw new Error(
        `Failed to list tools from '${this.spec.name}': ${error instanceof Error ? error.message : String(error)}`
      );
    }
  }

  /**
   * Call a tool on the server and return its result.
   * If the tool doesn't exist or errors, returns/throws a structured error.
   */
  async callTool(name: string, args: Record<string, unknown>): Promise<unknown> {
    if (this.client === null) {
      throw new Error(`McpHost '${this.spec.name}' is not started`);
    }

    try {
      const response = await this.client.callTool({
        name,
        arguments: args,
      });

      // Check if the response indicates an error
      if (response.isError) {
        throw new Error(`Tool '${name}' returned an error: ${JSON.stringify(response.content)}`);
      }

      // Return the tool result (the SDK's content array)
      return response.content;
    } catch (error) {
      if (error instanceof Error && error.message.includes('Unknown tool')) {
        throw new Error(`Tool '${name}' not found in server '${this.spec.name}'`);
      }
      throw error;
    }
  }

  /**
   * Stop the server connection and clean up resources.
   * Safe to call multiple times (idempotent).
   */
  async stop(): Promise<void> {
    if (this.client !== null) {
      try {
        await this.client.close();
      } catch (error) {
        console.error(`Error closing client for '${this.spec.name}':`, error);
      }
      this.client = null;
    }

    if (this.transport !== null) {
      try {
        this.transport.close();
      } catch (error) {
        console.error(`Error closing transport for '${this.spec.name}':`, error);
      }
      this.transport = null;
    }
  }
}
