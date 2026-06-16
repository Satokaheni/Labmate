import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { registerAllTools } from './registry.js';
import { log } from './services/logger.js';
async function main() {
    const server = new McpServer({ name: 'labmate', version: '0.1.0' });
    const transport = new StdioServerTransport();
    registerAllTools(server);
    let shuttingDown = false;
    const shutdown = async (sig) => {
        if (shuttingDown)
            return;
        shuttingDown = true;
        log.info({ sig }, 'shutting down');
        try {
            await server.close();
            await transport.close();
        }
        finally {
            process.exit(0);
        }
    };
    process.on('SIGINT', () => { void shutdown('SIGINT'); });
    process.on('SIGTERM', () => { void shutdown('SIGTERM'); });
    process.on('uncaughtException', (e) => { log.fatal(e, 'uncaught'); void shutdown('uncaughtException'); });
    await server.connect(transport);
    log.info('labmate MCP server ready on stdio');
}
main().catch((e) => { log.fatal(e, 'fatal startup'); process.exit(1); });
//# sourceMappingURL=index.js.map