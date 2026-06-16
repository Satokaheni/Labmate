import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import type { RequestHandlerExtra } from '@modelcontextprotocol/sdk/shared/protocol.js';
import type { ServerRequest, ServerNotification } from '@modelcontextprotocol/sdk/types.js';
import { ExecRunInput } from '../schemas/exec.js';
export declare function makeExecRunHandler(args: ExecRunInput, extra: RequestHandlerExtra<ServerRequest, ServerNotification>): Promise<{
    content: {
        type: 'text';
        text: string;
    }[];
    isError?: boolean;
}>;
export declare function registerExecTools(server: McpServer): void;
//# sourceMappingURL=exec.d.ts.map