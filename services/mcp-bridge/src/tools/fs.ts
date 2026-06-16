import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { readFile, readdir, writeFile } from 'node:fs/promises';
import { FsReadInput, FsListInput, FsWriteInput } from '../schemas/fs.js';
import { truncate } from '../utils/truncate.js';
import { log } from '../services/logger.js';

export async function makeReadHandler(
  args: FsReadInput,
): Promise<{ content: { type: 'text'; text: string }[]; isError?: true; structuredContent?: unknown }> {
  try {
    const content = await readFile(args.path, 'utf8');
    const { text, has_more, next_offset, total } = truncate(content, args.offset, args.limit);
    return {
      content: [{ type: 'text', text }],
      structuredContent: { has_more, next_offset, total },
    };
  } catch (err) {
    log.error({ err, path: args.path }, 'fs_read_file failed');
    const msg = err instanceof Error ? err.message : String(err);
    return { content: [{ type: 'text', text: `Error reading ${args.path}: ${msg}` }], isError: true };
  }
}

export async function makeWriteHandler(
  args: FsWriteInput,
): Promise<{ content: { type: 'text'; text: string }[]; isError?: true }> {
  try {
    await writeFile(args.path, args.content, 'utf8');
    return { content: [{ type: 'text', text: `Written ${args.content.length} chars to ${args.path}` }] };
  } catch (err) {
    log.error({ err, path: args.path }, 'fs_write_file failed');
    const msg = err instanceof Error ? err.message : String(err);
    return { content: [{ type: 'text', text: `Error writing ${args.path}: ${msg}` }], isError: true };
  }
}

export async function makeListHandler(
  args: FsListInput,
): Promise<{ content: { type: 'text'; text: string }[]; isError?: true }> {
  try {
    const entries = await readdir(args.path, { withFileTypes: true });
    const lines = entries.map(e => `${e.isDirectory() ? 'd' : 'f'} ${e.name}`);
    const { text } = truncate(lines.join('\n'));
    return { content: [{ type: 'text', text }] };
  } catch (err) {
    log.error({ err, path: args.path }, 'fs_list_dir failed');
    const msg = err instanceof Error ? err.message : String(err);
    return { content: [{ type: 'text', text: `Error listing ${args.path}: ${msg}` }], isError: true };
  }
}

export function registerFsTools(server: McpServer): void {
  server.registerTool(
    'fs_read_file',
    {
      title: 'Read file',
      description: 'Read a UTF-8 text file with character-offset pagination.',
      inputSchema: FsReadInput.shape,
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    makeReadHandler,
  );

  server.registerTool(
    'fs_list_dir',
    {
      title: 'List directory',
      description: 'List the immediate contents of a directory.',
      inputSchema: FsListInput.shape,
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    makeListHandler,
  );

  server.registerTool(
    'fs_write_file',
    {
      title: 'Write file',
      description: 'Write UTF-8 content to a file, creating or overwriting it.',
      inputSchema: FsWriteInput.shape,
      annotations: { readOnlyHint: false, openWorldHint: false },
    },
    makeWriteHandler,
  );
}
