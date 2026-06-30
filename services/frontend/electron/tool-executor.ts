import { promises as fs } from 'node:fs';
import path from 'node:path';
import { resolveToolPath } from './workspace';

export type LocalToolName = 'read_file' | 'write_file' | 'list_dir';
export const LOCAL_TOOL_NAMES: readonly LocalToolName[] = ['read_file', 'write_file', 'list_dir'];

/**
 * Execute a client-side file tool against the chat's workspace roots.
 * Relative paths resolve against the primary root (roots[0]); absolute paths
 * are allowed only inside one of the roots. See resolveToolPath.
 */
export async function executeTool(
  name: LocalToolName,
  args: Record<string, unknown>,
  roots: string[],
): Promise<unknown> {
  if (name === 'read_file') {
    const p = resolveToolPath(String(args.path ?? ''), roots);
    return { content: await fs.readFile(p, 'utf-8') };
  }
  if (name === 'write_file') {
    // Writes always target the primary root (roots[0]) for relative paths.
    const p = resolveToolPath(String(args.path ?? ''), roots);
    const content = String(args.content ?? '');
    await fs.mkdir(path.dirname(p), { recursive: true });
    await fs.writeFile(p, content, 'utf-8');
    return { ok: true, bytes: Buffer.byteLength(content, 'utf-8') };
  }
  if (name === 'list_dir') {
    const p = resolveToolPath(String(args.path ?? '.'), roots);
    const entries = await fs.readdir(p);
    return { entries };
  }
  throw new Error(`unknown local tool: ${String(name)}`);
}
