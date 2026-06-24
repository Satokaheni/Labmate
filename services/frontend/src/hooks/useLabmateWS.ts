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
}

type Action =
  | { type: 'CONNECTING' }
  | { type: 'AUTHENTICATING' }
  | { type: 'AUTH_ERROR'; reason: string }
  | { type: 'BOOT_PLAN'; subsystems: Subsystem[] }
  | { type: 'BOOT_UPDATE'; id: SubsystemId; state: SubsystemState; message?: string }
  | { type: 'BOOT_READY'; sessions: Session[]; activeSessionId: string | null; agentStatus: AgentStatus }
  | { type: 'TURN_CREATED'; turn: Turn }
  | { type: 'ANSWER_DELTA'; turnId: string; text: string }
  | { type: 'TURN_DONE'; turnId: string; status: 'complete' | 'error' }
  | { type: 'CONTEXT_UPDATE'; window: ContextWindow }
  | { type: 'AGENT_STATUS'; status: AgentStatus }
  | { type: 'SESSION_UPDATED'; session: Session }
  | { type: 'REASONING_DONE'; turnId: string; reasoning: Reasoning }
  | { type: 'ARTIFACT_CREATED'; turnId: string; artifact: Artifact }
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
};

function reducer(state: WsState, action: Action): WsState {
  switch (action.type) {
    case 'CONNECTING':
      return { ...INITIAL, phase: 'connecting' };
    case 'AUTHENTICATING':
      return { ...state, phase: 'authenticating' };
    case 'AUTH_ERROR':
      return { ...state, phase: 'error', authError: action.reason };
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
      return { ...state, turns: [...state.turns, action.turn] };
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
        sessions: exists
          ? state.sessions.map((s) => (s.id === action.session.id ? action.session : s))
          : [action.session, ...state.sessions],
      };
    }
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
    case 'CLOSED':
      return { ...state, phase: state.phase === 'ready' ? 'error' : 'idle' };
    default:
      return state;
  }
}

export function useLabmateWS(url: string, token: string | null, reconnectKey = 0) {
  const [state, dispatch] = useReducer(reducer, INITIAL);
  const wsRef = useRef<WebSocket | null>(null);

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
      const event = JSON.parse(ev.data) as StreamEvent;
      switch (event.type) {
        case 'auth.ok':
          break;
        case 'auth.error':
          dispatch({ type: 'AUTH_ERROR', reason: event.reason });
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
        case 'artifact.created':
          dispatch({ type: 'ARTIFACT_CREATED', turnId: event.turnId, artifact: event.artifact });
          break;
        case 'tool.request': {
          const reqId = event.toolRequestId;
          const reply = (result: unknown, error?: string) => {
            wsRef.current?.send(
              JSON.stringify({ type: 'tool.result', toolRequestId: reqId, result, error }),
            );
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

  return { state, send, newSession, openSession };
}
