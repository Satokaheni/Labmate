import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import type { RequestHandlerExtra } from '@modelcontextprotocol/sdk/shared/protocol.js';
import type { ServerRequest, ServerNotification } from '@modelcontextprotocol/sdk/types.js';
import { GitLogInput, GitStatusInput, GitDiffInput } from '../schemas/git.js';
export declare function makeStatusHandler(args: GitStatusInput, extra: RequestHandlerExtra<ServerRequest, ServerNotification>): Promise<{
    content: {
        type: 'text';
        text: string;
    }[];
    isError?: boolean;
}>;
export declare function makeLogHandler(args: GitLogInput, extra: RequestHandlerExtra<ServerRequest, ServerNotification>): Promise<{
    content: {
        type: 'text';
        text: string;
    }[];
    isError?: boolean;
}>;
export declare function makeDiffHandler(args: GitDiffInput, extra: RequestHandlerExtra<ServerRequest, ServerNotification>): Promise<{
    content: {
        type: 'text';
        text: string;
    }[];
    isError?: boolean;
}>;
export declare function registerGitTools(server: McpServer): void;
//# sourceMappingURL=git.d.ts.map