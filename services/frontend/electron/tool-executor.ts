import { promises as fs } from 'node:fs';
import path from 'node:path';

export type LocalToolName = 'read_file' | 'write_file' | 'list_dir';
export const LOCAL_TOOL_NAMES: readonly LocalToolName[] = ['read_file', 'write_file', 'list_dir'];

function safePath(rel: string, workspace: string): string {
  const root = path.resolve(workspace);
  const candidate = path.resolve(root, rel);
  const relToRoot = path.relative(root, candidate);
  const escapes = relToRoot.startsWith('..') || path.isAbsolute(relToRoot);
  if (candidate !== root && escapes) {
    throw new Error(`path "${rel}" resolves outside workspace`);
  }
  return candidate;
}

export async function executeTool(
  name: LocalToolName,
  args: Record<string, unknown>,
  workspace: string,
): Promise<unknown> {
  if (name === 'read_file') {
    const p = safePath(String(args.path ?? ''), workspace);
    return { content: await fs.readFile(p, 'utf-8') };
  }
  if (name === 'write_file') {
    const p = safePath(String(args.path ?? ''), workspace);
    const content = String(args.content ?? '');
    await fs.mkdir(path.dirname(p), { recursive: true });
    await fs.writeFile(p, content, 'utf-8');
    return { ok: true, bytes: Buffer.byteLength(content, 'utf-8') };
  }
  if (name === 'list_dir') {
    const p = safePath(String(args.path ?? '.'), workspace);
    const entries = await fs.readdir(p);
    return { entries };
  }
  throw new Error(`unknown local tool: ${String(name)}`);
}
