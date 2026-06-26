import { useCallback, useEffect, useReducer, useRef } from 'react';
import type {
  AgentStatus,
  Artifact,
  ContextWindow,
  Mode,
  Reasoning,
  Session,
  StreamEvent,
  Subsystem,
  SubsystemId,
  SubsystemState,
  Turn,
} from '@/types/events';

export type WsPhase = 'idle' | 'connecting' | 'authenticating' | 'booting' | 'ready' | 'error';

export interface WsState {
  phase: WsPhase;
  subsystems: Subsystem[];
  sessions: Session[];
  activeSessionId: string | null;
  agentStatus: AgentStatus | null;
  context: ContextWindow | null;
  turns: Turn[];
  authError: string | null;
  compacting: boolean;
}

type Action =
  | { type: 'CONNECTING' }
  | { type: 'AUTHENTICATING' }
  | { type: 'AUTH_ERROR'; reason: string }
  | { type: 'AUTH_ERROR_HANDLED' }
  | { type: 'BOOT_PLAN'; subsystems: Subsystem[] }
  | { type: 'BOOT_UPDATE'; id: SubsystemId; state: SubsystemState; message?: string }
  | { type: 'BOOT_READY'; sessions: Session[]; activeSessionId: string | null; agentStatus: AgentStatus }
  | { type: 'TURN_CREATED'; turn: Turn }
  | { type: 'ANSWER_DELTA'; turnId: string; text: string }
  | { type: 'TURN_DONE'; turnId: string; status: 'complete' | 'error' }
  | { type: 'CONTEXT_UPDATE'; window: ContextWindow }
  | { type: 'AGENT_STATUS'; status: AgentStatus }
  | { type: 'SESSION_UPDATED'; session: Session }
  | { type: 'SESSION_DELETED'; sessionId: string }
  | { type: 'REASONING_DONE'; turnId: string; reasoning: Reasoning }
  | { type: 'ARTIFACT_CREATED'; turnId: string; artifact: Artifact }
  | { type: 'TOOL_START'; turnId: string; toolCall: Omit<ToolCall, 'result' | 'durationMs' | 'status'> }
  | { type: 'TOOL_DONE'; turnId: string; toolId: string; status: ToolCall['status']; summary: string; result: unknown; durationMs: number }
  | { type: 'COMPACT_START' }
  | { type: 'COMPACT_DONE' }
  | { type: 'CLOSED' };

const INITIAL: WsState = {
  phase: 'idle',
  subsystems: [],
  sessions: [],
  activeSessionId: null,
  agentStatus: null,
  context: null,
  turns: [],
  authError: null,
  compacting: false,
};

function reducer(state: WsState, action: Action): WsState {
  switch (action.type) {
    case 'CONNECTING':
      return { ...INITIAL, phase: 'connecting' };
    case 'AUTHENTICATING':
      return { ...state, phase: 'authenticating' };
    case 'AUTH_ERROR':
      return { ...state, phase: 'error', authError: action.reason };
    case 'AUTH_ERROR_HANDLED':
      return { ...state, authError: null };
    case 'BOOT_PLAN':
      return { ...state, phase: 'booting', subsystems: action.subsystems };
    case 'BOOT_UPDATE':
      return {
        ...state,
        subsystems: state.subsystems.map((s) =>
          s.id === action.id
            ? { ...s, state: action.state, message: action.message ?? s.message }
            : s,
        ),
      };
    case 'BOOT_READY':
      return {
        ...state,
        phase: 'ready',
        sessions: action.sessions,
        activeSessionId: action.activeSessionId,
        agentStatus: action.agentStatus,
      };
    case 'TURN_CREATED':
      return {
        ...state,
        turns: [
          ...state.turns,
          action.turn.role === 'assistant'
            ? { ...action.turn, status: 'streaming' as const }
            : action.turn,
        ],
      };
    case 'ANSWER_DELTA':
      return {
        ...state,
        turns: state.turns.map((t) =>
          t.id === action.turnId ? { ...t, text: t.text + action.text } : t,
        ),
      };
    case 'TURN_DONE':
      return {
        ...state,
        turns: state.turns.map((t) =>
          t.id === action.turnId ? { ...t, status: action.status } : t,
        ),
      };
    case 'CONTEXT_UPDATE':
      return { ...state, context: action.window };
    case 'AGENT_STATUS':
      return { ...state, agentStatus: action.status };
    case 'SESSION_UPDATED': {
      const exists = state.sessions.some((s) => s.id === action.session.id);
      return {
        ...state,
        // Auto-activate brand-new sessions so a fresh newSession() call switches to it.
        activeSessionId: exists ? state.activeSessionId : action.session.id,
        sessions: exists
          ? state.sessions.map((s) => (s.id === action.session.id ? action.session : s))
          : [action.session, ...state.sessions],
      };
    }
    case 'SESSION_DELETED':
      return {
        ...state,
        sessions: state.sessions.filter((s) => s.id !== action.sessionId),
        activeSessionId:
          state.activeSessionId === action.sessionId ? null : state.activeSessionId,
      };
    case 'REASONING_DONE':
      return {
        ...state,
        turns: state.turns.map((t) =>
          t.id === action.turnId ? { ...t, reasoning: action.reasoning } : t,
        ),
      };
    case 'ARTIFACT_CREATED':
      return {
        ...state,
        turns: state.turns.map((t) =>
          t.id === action.turnId
            ? { ...t, artifacts: [...(t.artifacts ?? []), action.artifact] }
            : t,
        ),
      };
    case 'TOOL_START':
      return {
        ...state,
        turns: state.turns.map((t) =>
          t.id === action.turnId
            ? { ...t, toolCalls: [...(t.toolCalls ?? []), { ...action.toolCall, status: 'running' as const, result: null, durationMs: 0 }] }
            : t,
        ),
      };
    case 'TOOL_DONE':
      return {
        ...state,
        turns: state.turns.map((t) =>
          t.id === action.turnId
            ? {
                ...t,
                toolCalls: (t.toolCalls ?? []).map((tc) =>
                  tc.id === action.toolId
                    ? { ...tc, status: action.status, summary: action.summary, result: action.result, durationMs: action.durationMs }
                    : tc,
                ),
              }
            : t,
        ),
      };
    case 'COMPACT_START':
      return { ...state, compacting: true };
    case 'COMPACT_DONE':
      return { ...state, compacting: false };
    case 'CLOSED':
      return { ...state, phase: state.phase === 'ready' ? 'error' : 'idle', compacting: false };
    default:
      return state;
  }
}

export interface UseLabmateWSOptions {
  onAuthError?: (reason: string) => void;
}

export function useLabmateWS(url: string, token: string | null, reconnectKey = 0, options?: UseLabmateWSOptions) {
  const [state, dispatch] = useReducer(reducer, INITIAL);
  const wsRef = useRef<WebSocket | null>(null);
  const optionsRef = useRef(options);
  optionsRef.current = options;

  useEffect(() => {
    if (!token) return;

    dispatch({ type: 'CONNECTING' });
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      dispatch({ type: 'AUTHENTICATING' });
      ws.send(JSON.stringify({ type: 'auth', token }));
    };

    ws.onmessage = (ev: MessageEvent<string>) => {
      let event: StreamEvent;
      try {
        event = JSON.parse(ev.data) as StreamEvent;
      } catch {
        return;
      }
      switch (event.type) {
        case 'auth.ok':
          break;
        case 'auth.error':
          dispatch({ type: 'AUTH_ERROR', reason: event.reason });
          queueMicrotask(() => optionsRef.current?.onAuthError?.(event.reason));
          break;
        case 'boot.plan':
          dispatch({ type: 'BOOT_PLAN', subsystems: event.subsystems });
          break;
        case 'boot.update':
          dispatch({ type: 'BOOT_UPDATE', id: event.id, state: event.state, message: event.message });
          break;
        case 'boot.error':
          dispatch({ type: 'BOOT_UPDATE', id: event.id, state: 'failed', message: event.message });
          break;
        case 'boot.ready':
          dispatch({
            type: 'BOOT_READY',
            sessions: event.sessionBootstrap.sessions,
            activeSessionId: event.sessionBootstrap.activeSessionId,
            agentStatus: event.sessionBootstrap.agentStatus,
          });
          break;
        case 'turn.created':
          dispatch({ type: 'TURN_CREATED', turn: event.turn });
          break;
        case 'answer.delta':
          dispatch({ type: 'ANSWER_DELTA', turnId: event.turnId, text: event.text });
          break;
        case 'turn.done':
          dispatch({ type: 'TURN_DONE', turnId: event.turnId, status: event.status });
          break;
        case 'context.update':
          dispatch({ type: 'CONTEXT_UPDATE', window: event.window });
          break;
        case 'agent.status':
          dispatch({ type: 'AGENT_STATUS', status: event.status });
          break;
        case 'session.updated':
          dispatch({ type: 'SESSION_UPDATED', session: event.session });
          break;
        case 'reasoning.done':
          dispatch({ type: 'REASONING_DONE', turnId: event.turnId, reasoning: event.reasoning });
          break;
        case 'tool.start':
          dispatch({ type: 'TOOL_START', turnId: event.turnId, toolCall: event.toolCall });
          break;
        case 'tool.done':
          dispatch({ type: 'TOOL_DONE', turnId: event.turnId, toolId: event.toolId, status: event.status, summary: event.summary, result: event.result, durationMs: event.durationMs });
          break;
        case 'artifact.created':
          dispatch({ type: 'ARTIFACT_CREATED', turnId: event.turnId, artifact: event.artifact });
          break;
        case 'compact.done':
          dispatch({ type: 'COMPACT_DONE' });
          break;
        case 'compact.auto':
        case 'compact.micro':
          dispatch({ type: 'COMPACT_START' });
          break;
        case 'tool.request': {
          const reqId = event.toolRequestId;
          // Capture `ws` (not wsRef.current) so a late async reply goes to the
          // exact socket that issued the request, not a newer reconnected one.
          const replySocket = ws;
          const reply = (result: unknown, error?: string) => {
            if (replySocket.readyState === WebSocket.OPEN) {
              replySocket.send(
                JSON.stringify({ type: 'tool.result', toolRequestId: reqId, result, error }),
              );
            }
          };
          const api = window.electronAPI;
          if (!api) {
            reply(null, 'no local filesystem available (not running in Electron)');
            break;
          }
          void api
            .executeTool(event.name, event.args)
            .then((resp) => reply(resp.result ?? null, resp.error))
            .catch((err) =>
              reply(null, err instanceof Error ? err.message : String(err)),
            );
          break;
        }
      }
    };

    ws.onclose = () => {
      dispatch({ type: 'CLOSED' });
    };

    return () => {
      ws.onopen = null;
      ws.onmessage = null;
      ws.onclose = null;
      ws.close();
      wsRef.current = null;
    };
  }, [url, token, reconnectKey]);

  const send = useCallback((text: string, sessionId: string) => {
    wsRef.current?.send(JSON.stringify({ type: 'send', sessionId, mode: 'chat', text }));
  }, []);

  const newSession = useCallback((mode: Mode) => {
    wsRef.current?.send(JSON.stringify({ type: 'session.new', mode }));
  }, []);

  const openSession = useCallback((sessionId: string) => {
    wsRef.current?.send(JSON.stringify({ type: 'session.open', sessionId }));
  }, []);

  const setDebug = useCallback((sessionId: string, enabled: boolean) => {
    wsRef.current?.send(JSON.stringify({ type: 'debug.set', sessionId, enabled }));
  }, []);

  const compact = useCallback(() => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    dispatch({ type: 'COMPACT_START' });
    wsRef.current.send(JSON.stringify({ type: 'compact' }));
  }, []);

  const cancel = useCallback(() => {
    wsRef.current?.send(JSON.stringify({ type: 'cancel' }));
  }, []);

  const deleteSession = useCallback((sessionId: string) => {
    wsRef.current?.send(JSON.stringify({ type: 'session.delete', sessionId }));
    dispatch({ type: 'SESSION_DELETED', sessionId });
  }, []);

  const clearAuthError = useCallback(() => {
    dispatch({ type: 'AUTH_ERROR_HANDLED' });
  }, []);

  return { state, send, newSession, openSession, setDebug, compact, cancel, deleteSession, clearAuthError };
}
