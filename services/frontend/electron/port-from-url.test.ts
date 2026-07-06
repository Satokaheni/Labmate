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

  it('falls back to LOCAL_PORT env when wsUrl is null', () => {
    process.env.LOCAL_PORT = '9001';
    expect(portFromWsUrl(null)).toBe(9001);
  });
});
