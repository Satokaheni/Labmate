export function formatError(
  err: unknown,
  context: Record<string, unknown>,
): string {
  const e      = err instanceof Error ? err : new Error(String(err));
  const code   = (err as Record<string, unknown>)?.code;
  const stack  = e.stack
    ? e.stack.split('\n').slice(1, 5).join('\n')  // top 4 frames, skip message line
    : null;

  const parts: string[] = [
    `message: ${e.message}`,
    code                 ? `code: ${code}`                                  : null,
    `context: ${JSON.stringify(context)}`,
    stack                ? `stack:\n${stack}`                               : null,
  ].filter((x): x is string => x !== null);

  return parts.join('\n');
}
