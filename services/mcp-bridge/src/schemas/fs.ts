import { z } from 'zod';

export const FsReadInput = z.object({
  path: z.string().describe('Absolute path of the file to read.'),
  offset: z.number().int().min(0).default(0)
    .describe('Character offset to start reading (for pagination).'),
  limit: z.number().int().min(1).default(25_000)
    .describe('Max characters to return per call.'),
}).strict();

export const FsListInput = z.object({
  path: z.string().describe('Absolute path of the directory to list.'),
  depth: z.number().int().min(1).max(5).default(2)
    .describe('Max directory depth to traverse.'),
}).strict();

export const FsWriteInput = z.object({
  path: z.string().describe('Absolute path to write.'),
  content: z.string().describe('UTF-8 content to write to the file.'),
}).strict();

export type FsReadInput = z.infer<typeof FsReadInput>;
export type FsListInput = z.infer<typeof FsListInput>;
export type FsWriteInput = z.infer<typeof FsWriteInput>;
