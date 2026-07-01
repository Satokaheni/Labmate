import path from 'node:path';

/** Arg keys that hosted skills treat as filesystem paths.
 *  ast-ts-refactor uses: tsconfig, file, source_file, dest_file
 *  ast-repo-map uses: path
 *  code-sandbox uses: files, directory, dir
 *  component-doc-gen uses: component_path, dir_path
 *  a11y-audit uses: html_or_component_path
 */
export const PATH_ARG_KEYS = [
  'path',
  'file',
  'tsconfig',
  'source_file',
  'dest_file',
  'dir',
  'directory',
  'filepath',
  'file_path',
  'files',
  'paths',
  'project',
  'component_path',
  'dir_path',
  'html_or_component_path',
];

/** Resolve relative path-typed args to absolute against the primary workspace root.
 *  Absolute values pass through unchanged; non-path keys untouched; arrays mapped.
 *  Returns args unchanged when there is no root. Pure — does not mutate the input. */
export function resolveMcpPathArgs(
  args: Record<string, unknown>,
  roots: string[],
): Record<string, unknown> {
  if (!roots.length) return args;
  const root = roots[0];

  const one = (v: unknown) =>
    typeof v === 'string' && v.length > 0 && !path.isAbsolute(v)
      ? path.join(root, v)
      : v;

  const out: Record<string, unknown> = { ...args };
  for (const key of PATH_ARG_KEYS) {
    if (key in out) {
      const v = out[key];
      out[key] = Array.isArray(v) ? v.map(one) : one(v);
    }
  }
  return out;
}

/** Default `projectPath` to the active workspace root for CodeGraph tool calls, so a
 *  `codegraph_*` call targets the CHAT'S repo. CodeGraph resolves its project from
 *  `projectPath` (or its process cwd) — it does NOT consume MCP roots — so this is how
 *  a hosted CodeGraph follows the active workspace. An explicit `projectPath` from the
 *  model (e.g. to query a different repo) is respected. Pure — never mutates the input. */
export function injectCodegraphProjectPath(
  name: string,
  args: Record<string, unknown>,
  roots: string[],
): Record<string, unknown> {
  if (!name.startsWith('mcp__codegraph__')) return args;
  if (typeof args.projectPath === 'string' && args.projectPath.length > 0) return args;
  if (!roots.length) return args;
  return { ...args, projectPath: roots[0] };
}
