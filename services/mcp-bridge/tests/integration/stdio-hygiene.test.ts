import { describe, it, expect } from 'vitest';
import { spawn } from 'node:child_process';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SERVER_PATH = resolve(__dirname, '../../dist/index.js');

describe('stdout hygiene', () => {
  it('all stdout bytes from the server are valid JSON-RPC 2.0', async () => {
    const server = spawn('node', [SERVER_PATH], {
      stdio: ['pipe', 'pipe', 'pipe'],
    });

    const lines: string[] = [];
    server.stdout!.on('data', (chunk: Buffer) => {
      chunk.toString().split('\n').filter(Boolean).forEach(l => lines.push(l));
    });

    // Send MCP initialize request
    const initRequest = JSON.stringify({
      jsonrpc: '2.0',
      id: 1,
      method: 'initialize',
      params: {
        protocolVersion: '2024-11-05',
        capabilities: {},
        clientInfo: { name: 'test-client', version: '0.0.1' },
      },
    }) + '\n';

    server.stdin!.write(initRequest);

    // Wait 1.5s for server to respond
    await new Promise<void>(resolve => setTimeout(resolve, 1500));

    server.kill('SIGTERM');
    await new Promise<void>(resolve => server.on('close', resolve));

    // Must have received at least one response
    expect(lines.length).toBeGreaterThan(0);

    // Every line must be valid JSON with jsonrpc: '2.0'
    for (const line of lines) {
      let parsed: unknown;
      expect(
        () => { parsed = JSON.parse(line); },
        `Line is not valid JSON: ${line.substring(0, 100)}`,
      ).not.toThrow();
      expect((parsed as any).jsonrpc).toBe('2.0');
    }
  }, 15_000);
});
