import { McpServer }        from '@modelcontextprotocol/sdk/server/mcp.js';
import { registerFsTools }  from './tools/fs.js';
import { registerGitTools } from './tools/git.js';
import { registerExecTools } from './tools/exec.js';

export function registerAllTools(server: McpServer): void {
  registerFsTools(server);
  registerGitTools(server);
  registerExecTools(server);
}
