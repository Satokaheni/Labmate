import { createRoot } from 'react-dom/client';
import './styles/tokens.css';
import { ChatScreen } from './components/chat/ChatScreen';
import type { LabmateWSStatePublic } from './types/events';

/* Standalone visual-check harness for ChatScreen (mock data, no backend).
   NOT shipped — used only to screenshot against Labmate.dc.html. */

const state: LabmateWSStatePublic = {
  phase: 'ready',
  subsystems: [],
  activeSessionId: 'm3',
  agentStatus: {
    brain: { id: 'brain', state: 'active', model: 'gemma-4-31b', node: 'plan_node', thinkingBudget: 3000 },
    nervousSystem: { id: 'nervous_system', name: 'MCP bridge', state: 'connected', toolsRegistered: 3 },
    hands: { skills: [{ id: 'hands', name: 'ast-repo-map', state: 'active' }] },
  },
  sessions: [
    { id: 'm3', title: 'Milestone 3 — MCP bridge', mode: 'code', turnCount: 14 },
    { id: 'imrad', title: 'IMRaD: results section', mode: 'paper', turnCount: 9 },
    { id: 'llama', title: 'llama.cpp vs vLLM', mode: 'chat', turnCount: 6 },
    { id: 'codegraph', title: 'Codegraph AST repo-map', mode: 'code', turnCount: 21 },
  ],
  contextWindow: {
    used: 11597,
    max: 16384,
    free: 4787,
    segments: {
      systemPrompt: 1820,
      skillInstructions: 980,
      conversation: 4310,
      workingMemory: 1640,
      reasoning: 2847,
    },
  },
  turns: [
    {
      id: 't1',
      role: 'user',
      text: "Let's start Milestone 3 — implement the TypeScript MCP server in services/mcp-bridge. Wire up stdio JSON-RPC and register the existing skills.",
    },
    {
      id: 't2',
      role: 'assistant',
      text: 'Done — I scaffolded the server. It registers each skill from `services/skills/` and speaks JSON-RPC 2.0 over stdio, with the logger on **stderr**:',
      reasoning: {
        summary: 'planned the bridge',
        node: 'plan_node',
        text: 'Scaffolded the server with @modelcontextprotocol/sdk and chose the stdio transport. Critical: the logger must go to stderr — stdout carries JSON-RPC frames.',
        tokens: 2847,
        budget: 3000,
        durationMs: 2800,
      },
      toolCalls: [
        {
          id: 'c1',
          name: 'ast-repo-map',
          kind: 'skill',
          durationMs: 340,
          summary: 'scanned services/ — 18 files',
          reasoningWhy: 'I need the real shape of services/ before generating the registry so the server registers real tools rather than guesses.',
          args: { path: 'services/', depth: 2 },
          result: { dirs: 4, files: 18, manifests: 3 },
        },
        {
          id: 'c2',
          name: 'read_file',
          kind: 'tool',
          durationMs: 12,
          summary: 'read specs/spec_mcp_bridge.md',
          reasoningWhy: 'Confirm the transport and registration contract from the spec.',
          args: { path: 'specs/spec_mcp_bridge.md' },
          result: '1,204 tok · 64 lines read',
        },
        {
          id: 'c3',
          name: 'code_search',
          kind: 'tool',
          durationMs: 28,
          summary: 'code_search "ClientSession" — 3 hits',
          args: { query: 'ClientSession', scope: 'services/' },
          result: { hits: 3 },
        },
      ],
      artifacts: [
        {
          id: 'a1',
          name: 'server.ts',
          path: 'services/mcp-bridge/src/server.ts',
          language: 'TypeScript',
          mime: 'text/typescript',
          sizeBytes: 512,
          lineCount: 22,
          preview: 'code',
          content: 'import { Server } from "@modelcontextprotocol/sdk/server";\n// stdout is sacred — JSON-RPC only.\nconst skills = await loadSkills();',
        },
      ],
    },
    { id: 't3', role: 'user', text: 'Now add a test for the registry loader.' },
    {
      id: 't4',
      role: 'assistant',
      status: 'streaming',
      createdAt: new Date(Date.now() - 4200).toISOString(),
      text: '',
      toolCalls: [
        {
          id: 'c4',
          name: 'ast-repo-map',
          kind: 'skill',
          status: 'done',
          durationMs: 210,
          summary: 'located skillRegistry.ts',
        },
        { id: 'c5', name: 'run_tests', kind: 'tool', status: 'running', summary: 'pytest -q' },
      ],
    },
  ],
};

// Stub the Electron bridge so the workspace UI shows its active state in the
// browser harness (no effect on the real app, which provides the real bridge).
let stubRoots = ['/workspace/Labmate', '/workspace/hermes-agent'];
const stubEntries = [
  { absolute: '/workspace/Labmate/services/ws_gateway/server.py', insert: 'services/ws_gateway/server.py', display: 'services/ws_gateway/server.py', root: '/workspace/Labmate', isDir: false },
  { absolute: '/workspace/Labmate/services/orchestrator', insert: 'services/orchestrator', display: 'services/orchestrator', root: '/workspace/Labmate', isDir: true },
  { absolute: '/workspace/Labmate/services/skills/ast-repo-map/server.py', insert: 'services/skills/ast-repo-map/server.py', display: 'services/skills/ast-repo-map/server.py', root: '/workspace/Labmate', isDir: false },
];
(window as unknown as { electronAPI: unknown }).electronAPI = {
  config: { wsUrl: null, isDev: true },
  token: null,
  setConfig: async () => {},
  setToken: async () => {},
  clearToken: async () => {},
  executeTool: async () => ({ result: null }),
  getWorkspaceRoots: async () => stubRoots,
  addWorkspaceRoot: async () => { stubRoots = [...stubRoots, '/workspace/openclaw']; return { roots: stubRoots }; },
  removeWorkspaceRoot: async (_id: string, p: string) => { stubRoots = stubRoots.filter((r) => r !== p); return { roots: stubRoots }; },
  hasDefaultWorkspace: async () => true,
  getDefaultWorkspace: async () => stubRoots[0],
  setDefaultWorkspace: async () => ({ path: stubRoots[0] }),
  searchWorkspace: async (_id: string, q: string) => ({
    entries: stubEntries.filter((e) => e.display.toLowerCase().includes(q.toLowerCase())),
  }),
};

createRoot(document.getElementById('root')!).render(
  <ChatScreen state={state} send={() => {}} newSession={() => {}} newChat={() => 's-preview'} openSession={() => {}} setDebug={() => {}} />
);
