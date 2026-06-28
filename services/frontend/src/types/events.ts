// Shared event / view-model types for the Labmate frontend.
//
// These mirror the backend event stream (orchestrator -> ws_gateway -> client)
// and the view models the chat components render. The component *tests* encode
// the intended shapes, so these are the superset of fields used across both.
// Keep in sync with services/orchestrator/events.py + the ws_gateway frames.

/** LangGraph node / phase a reasoning step or tool ran under. Open string union. */
export type NodeName =
  | 'assess_ambiguity'
  | 'plan'
  | 'execute'
  | 'verify'
  | 'check'
  | 'reflect'
  | 'revise'
  | 'react'
  | (string & {});

/** One tool invocation within an assistant turn. */
export interface ToolCall {
  id: string;
  name: string;
  status?: 'running' | 'done' | 'error';
  kind?: string;
  args?: unknown;
  result?: unknown;
  summary?: string;
  reasoningWhy?: string;
  durationMs?: number;
}

/** A reasoning step emitted by a graph node. */
export interface Reasoning {
  summary: string;
  node: NodeName;
  text: string;
  tokens: number;
  budget: number;
  durationMs?: number;
}

/** A file/document artifact produced by a skill (artifact_created event). */
export interface Artifact {
  id: string;
  name: string;
  path: string;
  language: string;
  mime: string;
  sizeBytes: number;
  lineCount?: number;
  preview: 'doc' | 'code';
  content: string;
  downloadUrl?: string;
}

/** One conversation turn (user or assistant). */
export interface Turn {
  id: string;
  role: 'user' | 'assistant' | 'system';
  sessionId?: string;
  status?: string;
  createdAt?: string;
  text?: string;
  content?: string;
  toolCalls?: ToolCall[];
  reasoning?: Reasoning;
  artifacts?: Artifact[];
}

/** A chat session. */
export interface Session {
  id: string;
  title?: string;
  mode?: string;
  turnCount?: number;
  contextTokens?: number;
  createdAt?: string;
  updatedAt?: string;
  turns?: Turn[];
}

/** Health state of a subsystem dot (superset across footer + boot screen). */
export type SubsystemState =
  | 'active'
  | 'idle'
  | 'error'
  | 'connected'
  | 'pending'
  | 'starting'
  | 'ready'
  | 'degraded'
  | 'failed';

/** Identifier for a subsystem (matches the boot-screen Record keys). */
export type SubsystemId =
  | 'brain'
  | 'nervous_system'
  | 'hands'
  | 'memory'
  | 'workspace';

/** A single subsystem (brain / nervous system / a hand-skill / boot item). */
export interface Subsystem {
  id?: SubsystemId;
  name?: string;
  state: SubsystemState;
  endpoint?: string;
  transport?: string;
  label?: string;
  model?: string;
  node?: string;
  toolsRegistered?: number;
  thinkingBudget?: number;
  message?: string;
  detail?: string;
  required?: boolean;
}

/** Aggregate agent status rendered in the system footer. */
export interface AgentStatus {
  brain: Subsystem;
  nervousSystem: Subsystem;
  hands: {
    skills: Subsystem[];
  };
}

/** Per-segment token accounting for the context-window bar. */
export interface ContextWindow {
  used: number;
  max: number;
  free?: number;
  segments: {
    systemPrompt: number;
    skillInstructions: number;
    conversation: number;
    workingMemory: number;
    reasoning: number;
  };
}

/** Bootstrap data sent on boot.ready. */
export interface SessionBootstrap {
  sessions: Session[];
  activeSessionId: string | null;
  agentStatus: AgentStatus;
}

/** Extended phase type with all optional properties for hook state access. */
export type LabmateWSPhase = 'idle' | 'connecting' | 'authenticating' | 'booting' | 'ready' | 'error';

/** Public type for WebSocket hook state - all properties present but may be undefined. */
export type LabmateWSStatePublic = {
  phase: LabmateWSPhase;
  subsystems: Subsystem[];
  agentStatus: AgentStatus;
  sessions: Session[];
  activeSessionId: string | null;
  turns: Turn[];
  contextWindow?: ContextWindow;
  authError?: string;
};

/** Discriminated union of streamed events the chat UI reduces over. */
export type StreamEvent =
  | { type: 'node.enter'; node?: NodeName; thinkingBudget?: number; turnId?: string }
  | { type: 'reasoning.delta'; text?: string; turnId?: string }
  | { type: 'reasoning.done'; reasoning: Reasoning; turnId?: string }
  | { type: 'tool.start'; toolCall: ToolCall; turnId?: string }
  | { type: 'tool.done'; toolId: NodeName | string; summary?: string; turnId?: string }
  | { type: 'answer.delta'; text?: string; turnId?: string }
  | { type: 'answer.done'; text?: string; turnId?: string }
  | { type: 'turn.done'; status?: string; turnId?: string };
