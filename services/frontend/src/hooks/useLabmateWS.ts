import { useEffect, useReducer, useRef } from 'react';
import type {
  Subsystem,
  SubsystemState,
  Session,
  Turn,
  AgentStatus,
  ContextWindow,
  Artifact,
  Reasoning,
  SessionBootstrap,
  LabmateWSStatePublic,
} from '@/types/events';

type LabmateWSStateBase = {
  subsystems?: Subsystem[];
  agentStatus?: AgentStatus;
  sessions?: Session[];
  activeSessionId?: string | null;
  turns?: Turn[];
  contextWindow?: ContextWindow;
  authError?: string;
};

export type LabmateWSState = LabmateWSStateBase & (
  | { phase: 'idle' }
  | { phase: 'connecting' }
  | { phase: 'authenticating' }
  | { phase: 'booting'; subsystems: Subsystem[]; turns?: Turn[] }
  | {
      phase: 'ready';
      subsystems: Subsystem[];
      agentStatus: AgentStatus;
      sessions: Session[];
      activeSessionId: string | null;
      turns: Turn[];
    }
  | { phase: 'error' }
);

type WSFrame =
  | { type: 'auth.error'; reason: string }
  | { type: 'auth.ok' }
  | { type: 'boot.plan'; subsystems: Subsystem[] }
  | { type: 'boot.update'; id: string; state: string }
  | { type: 'boot.ready'; sessionBootstrap: SessionBootstrap }
  | { type: 'turn.created'; turn: Turn }
  | { type: 'answer.delta'; turnId: string; text: string }
  | { type: 'reasoning.done'; turnId: string; reasoning: Reasoning }
  | { type: 'artifact.created'; turnId: string; artifact: Artifact }
  | { type: 'session.updated'; session: Session }
  | { type: 'context.update'; window: ContextWindow }
  | { type: 'agent.status'; status: AgentStatus }
  | { type: 'turn.done'; turnId: string; status: string }
  | { type: 'tool.request'; turnId: string; toolRequestId: string; name: string; args: unknown }
  | { type: '_INTERNAL_RESET' };

type DispatchAction =
  | { action: 'CONNECTING' }
  | { action: 'AUTHENTICATING' }
  | { action: 'FRAME'; frame: WSFrame }
  | { action: 'ERROR'; error: string }
  | { action: 'RESET' };

function labmateWSReducer(state: LabmateWSState, action: DispatchAction): LabmateWSState {
  switch (action.action) {
    case 'CONNECTING':
      return { phase: 'connecting' };

    case 'AUTHENTICATING':
      return { phase: 'authenticating' };

    case 'ERROR': {
      return { phase: 'error', authError: action.error };
    }

    case 'RESET':
      return { phase: 'idle' };

    case 'FRAME': {
      const frame = action.frame;

      if (frame.type === 'auth.error') {
        return { phase: 'error', authError: frame.reason };
      }

      if (frame.type === 'auth.ok') {
        return state; // remain authenticating, wait for boot
      }

      if (frame.type === 'boot.plan') {
        if (state.phase === 'authenticating') {
          return { phase: 'booting', subsystems: frame.subsystems };
        }
        // Update subsystems if already booting/ready
        if (state.phase === 'booting') {
          return { ...state, subsystems: frame.subsystems };
        }
        if (state.phase === 'ready') {
          return { ...state, subsystems: frame.subsystems };
        }
        return state;
      }

      if (frame.type === 'boot.update') {
        if (state.phase === 'booting' || state.phase === 'ready') {
          const updated = state.subsystems.map((s) =>
            s.id === frame.id ? { ...s, state: frame.state as SubsystemState } : s
          );
          if (state.phase === 'booting') {
            return { ...state, subsystems: updated };
          } else {
            return { ...state, subsystems: updated };
          }
        }
        return state;
      }

      if (frame.type === 'boot.ready') {
        if (state.phase === 'booting' || state.phase === 'authenticating') {
          return {
            phase: 'ready',
            subsystems: state.phase === 'booting' ? state.subsystems : [],
            agentStatus: frame.sessionBootstrap.agentStatus,
            sessions: frame.sessionBootstrap.sessions,
            activeSessionId: frame.sessionBootstrap.activeSessionId,
            turns: [],
          };
        }
        if (state.phase === 'ready') {
          return {
            ...state,
            agentStatus: frame.sessionBootstrap.agentStatus,
            sessions: frame.sessionBootstrap.sessions,
            activeSessionId: frame.sessionBootstrap.activeSessionId,
          };
        }
        return state;
      }

      if (frame.type === 'turn.created') {
        if (state.phase === 'booting' || state.phase === 'ready') {
          const turns = [...(state.turns ?? []), frame.turn];
          return { ...state, turns };
        }
        return state;
      }

      if (frame.type === 'answer.delta') {
        if (state.phase === 'booting' || state.phase === 'ready') {
          const turns = (state.turns ?? []).map((t) =>
            t.id === frame.turnId ? { ...t, text: (t.text ?? '') + frame.text } : t
          );
          return { ...state, turns };
        }
        return state;
      }

      if (frame.type === 'reasoning.done') {
        if (state.phase === 'booting' || state.phase === 'ready') {
          const turns = (state.turns ?? []).map((t) =>
            t.id === frame.turnId ? { ...t, reasoning: frame.reasoning } : t
          );
          return { ...state, turns };
        }
        return state;
      }

      if (frame.type === 'artifact.created') {
        if (state.phase === 'booting' || state.phase === 'ready') {
          const turns = (state.turns ?? []).map((t) => {
            if (t.id === frame.turnId) {
              return { ...t, artifacts: [...(t.artifacts ?? []), frame.artifact] };
            }
            return t;
          });
          return { ...state, turns };
        }
        return state;
      }

      if (frame.type === 'session.updated') {
        if (state.phase === 'ready') {
          const existing = state.sessions.findIndex((s) => s.id === frame.session.id);
          let sessions: Session[];
          if (existing >= 0) {
            // Update in place
            sessions = state.sessions.map((s, i) => (i === existing ? frame.session : s));
          } else {
            // Add new
            sessions = [...state.sessions, frame.session];
          }
          return { ...state, sessions };
        }
        return state;
      }

      if (frame.type === 'context.update') {
        if (state.phase === 'ready') {
          return { ...state, contextWindow: frame.window };
        }
        return state;
      }

      if (frame.type === 'agent.status') {
        if (state.phase === 'ready') {
          return { ...state, agentStatus: frame.status };
        }
        return state;
      }

      if (frame.type === 'turn.done') {
        if (state.phase === 'ready') {
          const turns = state.turns.map((t) =>
            t.id === frame.turnId ? { ...t, status: frame.status } : t
          );
          return { ...state, turns };
        }
        return state;
      }

      if (frame.type === 'tool.request') {
        // Handled in effect, not in reducer
        return state;
      }

      return state;
    }

    default:
      return state;
  }
}

export function useLabmateWS(
  wsUrl: string,
  token: string | null,
  reconnectKey?: number
): {
  state: LabmateWSStatePublic;
  send: (text: string, sessionId: string) => void;
  newSession?: (mode: string) => void;
  openSession?: (sessionId: string) => void;
  setDebug: (sessionId: string, enabled: boolean) => void;
} {
  const [state, dispatch] = useReducer(labmateWSReducer, { phase: 'idle' });
  const wsRef = useRef<WebSocket | null>(null);

  // Socket lifecycle: open when token is set, close/reopen on reconnectKey
  useEffect(() => {
    if (token === null) {
      // Close existing socket and go idle
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
      dispatch({ action: 'RESET' });
      return;
    }

    // Token is set; open a socket
    dispatch({ action: 'CONNECTING' });

    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => {
      dispatch({ action: 'AUTHENTICATING' });
      ws.send(JSON.stringify({ type: 'auth', token }));
    };

    ws.onmessage = (ev) => {
      try {
        const frame = JSON.parse(ev.data) as WSFrame;

        // Handle tool.request asynchronously (outside reducer)
        if (frame.type === 'tool.request') {
          handleToolRequest(frame, ws);
          return;
        }

        dispatch({ action: 'FRAME', frame });
      } catch (err) {
        console.error('Failed to parse WS frame:', err);
      }
    };

    ws.onclose = () => {
      wsRef.current = null;
    };

    return () => {
      if (wsRef.current === ws) {
        ws.close();
        wsRef.current = null;
      }
    };
  }, [wsUrl, token, reconnectKey]);

  const handleToolRequest = async (
    frame: Extract<WSFrame, { type: 'tool.request' }>,
    ws: WebSocket
  ) => {
    const { toolRequestId, name, args } = frame;

    try {
      const electronAPI = (window as Window & { electronAPI?: unknown }).electronAPI as
        | { executeTool: (name: string, args: unknown) => Promise<{ result: unknown }> }
        | undefined;

      if (!electronAPI) {
        ws.send(
          JSON.stringify({
            type: 'tool.result',
            toolRequestId,
            error: 'no local filesystem access — electronAPI not available',
          })
        );
        return;
      }

      const res = await electronAPI.executeTool(name, args);
      ws.send(
        JSON.stringify({
          type: 'tool.result',
          toolRequestId,
          result: res.result,
          error: undefined,
        })
      );
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      ws.send(
        JSON.stringify({
          type: 'tool.result',
          toolRequestId,
          error: msg,
        })
      );
    }
  };

  const send = (text: string, sessionId: string) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'send', sessionId, mode: 'chat', text }));
    }
  };

  const setDebug = (sessionId: string, enabled: boolean) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'debug.set', sessionId, enabled }));
    }
  };

  const newSession = (mode: string) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'session.new', mode }));
    }
  };

  const openSession = (sessionId: string) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'session.open', sessionId }));
    }
  };

  return {
    state: state as LabmateWSStatePublic,
    send,
    newSession,
    openSession,
    setDebug,
  };
}
