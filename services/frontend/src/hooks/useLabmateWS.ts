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
import { capabilitiesFrame, type ToolDescriptor } from '@/protocol/capabilities';

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
  | { type: 'session.deleted'; sessionId: string }
  | { type: 'session.history'; sessionId: string; turns: Turn[] }
  | { type: 'context.update'; window: ContextWindow }
  | { type: 'agent.status'; status: AgentStatus }
  | { type: 'turn.done'; turnId: string; status: string }
  | { type: 'tool.request'; turnId: string; toolRequestId: string; name: string; args: unknown }
  | {
      type: 'tool.start';
      turnId: string;
      toolCall: { id: string; name: string; kind?: string; summary?: string; reasoningWhy?: string; args?: unknown };
    }
  | {
      type: 'tool.done';
      turnId: string;
      toolId: string;
      status?: string;
      summary?: string;
      result?: unknown;
      durationMs?: number;
    }
  | { type: '_INTERNAL_RESET' };

type DispatchAction =
  | { action: 'CONNECTING' }
  | { action: 'AUTHENTICATING' }
  | { action: 'FRAME'; frame: WSFrame }
  | { action: 'ERROR'; error: string }
  | { action: 'SET_ACTIVE_SESSION'; sessionId: string | null }
  | { action: 'RESET' };

/** Mint a client-side session id (no backend round-trip needed). */
export function mintSessionId(): string {
  return (
    's-' +
    (typeof crypto !== 'undefined' && crypto.randomUUID
      ? crypto.randomUUID().replace(/-/g, '').slice(0, 12)
      : Math.random().toString(36).slice(2, 14))
  );
}

/**
 * Guarantee the boot bootstrap carries an active session id, so the view never
 * has to create one reactively (this replaces a newChat()-in-useEffect side
 * effect in ChatScreen — which risked a stale-closure / StrictMode double
 * create). Mints a fresh id only when the server delivered no active session
 * AND there are no existing sessions to fall back to. Idempotent: a bootstrap
 * that already names a session (or has sessions) is returned unchanged.
 */
export function ensureActiveSession(
  bootstrap: SessionBootstrap,
  mint: () => string = mintSessionId,
): SessionBootstrap {
  if (bootstrap.activeSessionId) return bootstrap;
  if (bootstrap.sessions.length > 0) return bootstrap;
  return { ...bootstrap, activeSessionId: mint() };
}

function labmateWSReducer(state: LabmateWSState, action: DispatchAction): LabmateWSState {
  switch (action.action) {
    case 'CONNECTING':
      return { phase: 'connecting' };

    case 'AUTHENTICATING':
      return { phase: 'authenticating' };

    case 'ERROR': {
      return { phase: 'error', authError: action.error };
    }

    case 'SET_ACTIVE_SESSION':
      if (state.phase === 'ready') {
        return { ...state, activeSessionId: action.sessionId };
      }
      return state;

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
          // Remove any existing entry for that id and unshift the updated session to the front
          const rest = state.sessions.filter((s) => s.id !== frame.session.id);
          return { ...state, sessions: [frame.session, ...rest] };
        }
        return state;
      }

      if (frame.type === 'session.history') {
        if (state.phase === 'ready') {
          // Keep only turns whose sessionId is absent or matches the incoming session,
          // then merge frame.turns deduped by id. This ensures switching sessions
          // cleanly drops the previous session's turns.
          const sid = frame.sessionId;
          const kept = (state.turns ?? []).filter((t) => !t.sessionId || t.sessionId === sid);
          const seen = new Set(kept.map((t) => t.id));
          const merged = [...kept, ...frame.turns.filter((t) => !seen.has(t.id))];
          return { ...state, turns: merged };
        }
        return state;
      }

      if (frame.type === 'session.deleted') {
        if (state.phase === 'ready') {
          const sessions = state.sessions.filter((s) => s.id !== frame.sessionId);
          let activeSessionId = state.activeSessionId;
          if (frame.sessionId === activeSessionId) {
            activeSessionId = sessions[0]?.id ?? null;
          }
          return { ...state, sessions, activeSessionId };
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

      if (frame.type === 'tool.start') {
        if (state.phase === 'booting' || state.phase === 'ready') {
          const turns = (state.turns ?? []).map((t) =>
            t.id === frame.turnId
              ? {
                  ...t,
                  toolCalls: [
                    ...(t.toolCalls ?? []),
                    {
                      id: frame.toolCall.id,
                      name: frame.toolCall.name,
                      kind: frame.toolCall.kind,
                      summary: frame.toolCall.summary,
                      reasoningWhy: frame.toolCall.reasoningWhy,
                      args: frame.toolCall.args,
                      status: 'running' as const,
                    },
                  ],
                }
              : t
          );
          return { ...state, turns };
        }
        return state;
      }

      if (frame.type === 'tool.done') {
        if (state.phase === 'booting' || state.phase === 'ready') {
          const turns = (state.turns ?? []).map((t) =>
            t.id === frame.turnId
              ? {
                  ...t,
                  toolCalls: (t.toolCalls ?? []).map((c) =>
                    c.id === frame.toolId
                      ? {
                          ...c,
                          status: frame.status === 'error' ? ('error' as const) : ('done' as const),
                          result: frame.result,
                          summary: frame.summary || c.summary,
                          durationMs: frame.durationMs,
                        }
                      : c
                  ),
                }
              : t
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
  send: (text: string, sessionId: string, workspaceRoot?: string) => void;
  newSession?: (mode: string) => void;
  newChat: () => string;
  setActiveSession: (sessionId: string) => void;
  openSession?: (sessionId: string) => void;
  setDebug: (sessionId: string, enabled: boolean) => void;
  renameSession: (sessionId: string, title: string) => void;
  deleteSession: (sessionId: string) => void;
  cancel: (turnId: string) => void;
} {
  const [state, dispatch] = useReducer(labmateWSReducer, { phase: 'idle' });
  const wsRef = useRef<WebSocket | null>(null);
  // Maps an assistant turnId -> its sessionId, so a tool.request (which carries
  // only turnId) can be executed against the right chat's local workspace.
  const turnSessionRef = useRef<Record<string, string>>({});

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

        // Remember which session each turn belongs to (for per-chat tool routing).
        if (frame.type === 'turn.created' && frame.turn?.id) {
          turnSessionRef.current[frame.turn.id] = frame.turn.sessionId ?? '';
        }

        // Also populate turnSessionRef for turns replayed via session.history
        if (frame.type === 'session.history') {
          for (const turn of frame.turns) {
            if (turn?.id) {
              turnSessionRef.current[turn.id] = turn.sessionId ?? frame.sessionId ?? '';
            }
          }
        }

        // Handle tool.request asynchronously (outside reducer)
        if (frame.type === 'tool.request') {
          handleToolRequest(frame, ws);
          return;
        }

        // Send capabilities frame after auth succeeds, including MCP tools and skills
        if (frame.type === 'auth.ok') {
          const sendCapabilities = async () => {
            try {
              const electronAPI = (window as unknown as {
                electronAPI?: {
                  getMcpTools?: () => Promise<ToolDescriptor[]>;
                  getSkillDescriptors?: () => Promise<ToolDescriptor[]>;
                };
              }).electronAPI;
              const mcpTools = (await electronAPI?.getMcpTools?.()) ?? [];
              const skillTools = (await electronAPI?.getSkillDescriptors?.()) ?? [];
              const allTools = [...mcpTools, ...skillTools];
              ws.send(JSON.stringify(capabilitiesFrame(allTools)));
            } catch (err) {
              console.error('Failed to fetch tools:', err);
              // Send capabilities with just builtins if fetch fails
              ws.send(JSON.stringify(capabilitiesFrame([])));
            }
          };
          void sendCapabilities();
          return; // Don't dispatch yet; let auth flow through after capabilities are sent
        }

        // Fill in a fresh active session at the source when boot delivers none,
        // so ChatScreen never has to create one from a useEffect.
        const outFrame =
          frame.type === 'boot.ready'
            ? { ...frame, sessionBootstrap: ensureActiveSession(frame.sessionBootstrap) }
            : frame;

        dispatch({ action: 'FRAME', frame: outFrame });
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
    const { toolRequestId, turnId, name, args } = frame;
    const sessionId = turnSessionRef.current[turnId] ?? null;

    try {
      const electronAPI = (window as Window & { electronAPI?: unknown }).electronAPI as
        | {
            executeTool: (
              name: string,
              args: unknown,
              sessionId?: string | null,
            ) => Promise<{ result?: unknown; error?: string }>;
          }
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

      const res = await electronAPI.executeTool(name, args, sessionId);
      ws.send(
        JSON.stringify({
          type: 'tool.result',
          toolRequestId,
          result: res.result,
          error: res.error,
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

  const send = (text: string, sessionId: string, workspaceRoot?: string) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'send', sessionId, mode: 'chat', text, ...(workspaceRoot ? { workspaceRoot } : {}) }));
    }
  };

  const setDebug = (sessionId: string, enabled: boolean) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'debug.set', sessionId, enabled }));
    }
  };

  const renameSession = (sessionId: string, title: string) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'session.rename', sessionId, title }));
    }
  };

  const deleteSession = (sessionId: string) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'session.delete', sessionId }));
    }
  };

  const newSession = (mode: string) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'session.new', mode }));
    }
  };

  const openSession = (sessionId: string) => {
    dispatch({ action: 'SET_ACTIVE_SESSION', sessionId });
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'session.open', sessionId }));
    }
  };

  // Start a fresh chat: mint a client-side session id and make it active. The
  // backend auto-creates + titles the session from the first message on send,
  // so no round-trip is needed before the user can type.
  const setActiveSession = (sessionId: string) => {
    dispatch({ action: 'SET_ACTIVE_SESSION', sessionId });
  };

  const newChat = (): string => {
    const id = mintSessionId();
    dispatch({ action: 'SET_ACTIVE_SESSION', sessionId: id });
    return id;
  };

  const cancel = (turnId: string) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'cancel', turnId }));
    }
  };

  return {
    state: state as LabmateWSStatePublic,
    send,
    newSession,
    newChat,
    setActiveSession,
    openSession,
    setDebug,
    renameSession,
    deleteSession,
    cancel,
  };
}
