import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import type { RequestHandlerExtra } from '@modelcontextprotocol/sdk/shared/protocol.js';
import type { ServerRequest, ServerNotification } from '@modelcontextprotocol/sdk/types.js';
import { FsReadInput, FsListInput, FsWriteInput } from '../schemas/fs.js';
export declare function makeReadHandler(args: FsReadInput, extra: RequestHandlerExtra<ServerRequest, ServerNotification>): Promise<{
    content: {
        type: 'text';
        text: string;
    }[];
    isError?: boolean;
    structuredContent?: Record<string, unknown>;
}>;
export declare function makeWriteHandler(args: FsWriteInput, extra: RequestHandlerExtra<ServerRequest, ServerNotification>): Promise<{
    content: {
        type: 'text';
        text: string;
    }[];
    isError?: boolean;
}>;
export declare function makeListHandler(args: FsListInput, extra: RequestHandlerExtra<ServerRequest, ServerNotification>): Promise<{
    content: {
        type: 'text';
        text: string;
    }[];
    isError?: boolean;
}>;
export declare function registerFsTools(server: McpServer): void;
//# sourceMappingURL=fs.d.ts.map