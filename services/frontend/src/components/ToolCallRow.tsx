import type { ToolCall } from '@/types/events';
export function ToolCallRow({ toolCall }: { toolCall: ToolCall }) {
  return <div data-testid="tool-call-row">{toolCall.name}</div>;
}
