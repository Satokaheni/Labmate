import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { portFromWsUrl } from './port-from-url';

describe('portFromWsUrl', () => {
  const origLocalPort = process.env.LOCAL_PORT;

  beforeEach(() => {
    delete process.env.LOCAL_PORT;
  });

  afterEach(() => {
    if (origLocalPort === undefined) delete process.env.LOCAL_PORT;
    else process.env.LOCAL_PORT = origLocalPort;
  });

  it('parses the port from a ws:// URL', () => {
    expect(portFromWsUrl('ws://localhost:8788/ws')).toBe(8788);
  });

  it('parses the port from a wss:// URL', () => {
    expect(portFromWsUrl('wss://host:443/ws')).toBe(443);
  });

  it('falls back to 8787 when wsUrl is null and LOCAL_PORT is unset', () => {
    expect(portFromWsUrl(null)).toBe(8787);
  });

  it('falls back to 8787 when wsUrl is not a valid URL', () => {
    expect(portFromWsUrl('not a url')).toBe(8787);
  });

  it('falls back to LOCAL_PORT/8787 for a URL with NO explicit port (not 80/443)', () => {
    // A no-port gateway URL must NOT resolve to 80/443 (would EACCES on bind).
    expect(portFromWsUrl('ws://localhost/ws')).toBe(8787);
    expect(portFromWsUrl('wss://pod.example.com/ws')).toBe(8787);
  });

  it('honors LOCAL_PORT for a no-explicit-port URL', () => {
    process.env.LOCAL_PORT = '8788';
    expect(portFromWsUrl('ws://localhost/ws')).toBe(8788);
  });

  it('falls back to LOCAL_PORT env when wsUrl is null', () => {
    process.env.LOCAL_PORT = '9001';
    expect(portFromWsUrl(null)).toBe(9001);
  });
});
