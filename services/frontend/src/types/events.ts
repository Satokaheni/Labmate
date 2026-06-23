export type Mode = 'chat' | 'paper' | 'code';

export type NodeName =
  | 'plan_node'
  | 'execute_node'
  | 'check_node'
  | 'reflect_node'
  | 'chat_node';

export interface Reasoning {
  summary: string;
  text: string;
  node: NodeName;
  tokens: number;
  budget: number;
  durationMs: number;
}

export interface ToolCall {
  id: string;
  name: string;
  kind: 'skill' | 'tool';
  status: 'running' | 'done' | 'error';
  summary: string;
  durationMs: number;
  reasoningWhy: string;
  args: unknown;
  result: unknown;
  trace?: unknown;
}

export interface Artifact {
  id: string;
  name: string;
  path: string;
  language: string;
  mime: string;
  sizeBytes: number;
  lineCount?: number;
  preview: 'code' | 'doc';
  content: string;
  downloadUrl: string;
}

export interface Turn {
  id: string;
  sessionId: string;
  role: 'user' | 'assistant';
  text: string;
  createdAt: string;
  reasoning?: Reasoning;
  toolCalls?: ToolCall[];
  artifacts?: Artifact[];
  status?: 'streaming' | 'complete' | 'error';
}

export interface Session {
  id: string;
  title: string;
  mode: Mode;
  turnCount: number;
  contextTokens: number;
  updatedAt: string;
  createdAt: string;
}

export interface ContextWindow {
  max: number;
  used: number;
  segments: {
    systemPrompt: number;
    skillInstructions: number;
    conversation: number;
    workingMemory: number;
    reasoning: number;
  };
  free: number;
}

export interface AgentStatus {
  brain: {
    model: string;
    endpoint: string;
    state: 'idle' | 'active' | 'error';
    node: NodeName;
    thinkingBudget: number;
  };
  nervousSystem: {
    name: 'MCP bridge';
    transport: string;
    state: 'connected' | 'disconnected' | 'error';
    toolsRegistered: number;
  };
  hands: { skills: Array<{ name: string; state: 'idle' | 'active' | 'done' | 'error' }> };
  memory?: { mongoMessages: number; chromaVectors: number; redisQueueDepth: number };
}

export interface AuthUser {
  id: string;
  email: string;
  displayName: string;
  createdAt: string;
}

export interface AuthSession {
  token: string;
  user: AuthUser;
  expiresAt: string;
}

export type SubsystemId =
  | 'brain'
  | 'nervous_system'
  | 'hands'
  | 'memory'
  | 'workspace';

export type SubsystemState =
  | 'pending'
  | 'starting'
  | 'ready'
  | 'degraded'
  | 'failed';

export interface Subsystem {
  id: SubsystemId;
  label: string;
  detail: string;
  state: SubsystemState;
  required: boolean;
  message?: string;
}

export interface SessionBootstrap {
  sessions: Session[];
  activeSessionId: string | null;
  agentStatus: AgentStatus;
}

export type StreamEvent =
  | { type: 'auth.ok'; user: AuthUser }
  | { type: 'auth.error'; reason: 'expired' | 'invalid' }
  | { type: 'boot.plan'; subsystems: Subsystem[] }
  | {
      type: 'boot.update';
      id: SubsystemId;
      state: SubsystemState;
      detail?: string;
      message?: string;
    }
  | { type: 'boot.ready'; sessionBootstrap: SessionBootstrap }
  | { type: 'boot.error'; id: SubsystemId; message: string }
  | { type: 'turn.created'; turn: Turn }
  | { type: 'node.enter'; turnId: string; node: NodeName; thinkingBudget: number }
  | { type: 'reasoning.delta'; turnId: string; text: string }
  | { type: 'reasoning.done'; turnId: string; reasoning: Reasoning }
  | {
      type: 'tool.start';
      turnId: string;
      toolCall: Omit<ToolCall, 'result' | 'durationMs' | 'status'>;
    }
  | {
      type: 'tool.done';
      turnId: string;
      toolId: string;
      status: ToolCall['status'];
      summary: string;
      result: unknown;
      durationMs: number;
    }
  | { type: 'answer.delta'; turnId: string; text: string }
  | { type: 'artifact.created'; turnId: string; artifact: Artifact }
  | { type: 'turn.done'; turnId: string; status: 'complete' | 'error' }
  | { type: 'context.update'; window: ContextWindow }
  | { type: 'agent.status'; status: AgentStatus }
  | { type: 'session.updated'; session: Session };

export type ClientMsg =
  | { type: 'auth'; token: string }
  | { type: 'send'; sessionId: string; mode: Mode; text: string }
  | { type: 'session.new'; mode: Mode }
  | { type: 'session.open'; sessionId: string }
  | { type: 'session.rename'; sessionId: string; title: string }
  | { type: 'debug.set'; sessionId: string; enabled: boolean }
  | { type: 'cancel'; sessionId: string; turnId: string };
