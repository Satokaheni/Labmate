import { describe, it, expect } from 'vitest';

describe('logger', () => {
  it('exports a pino logger with info, error, and fatal methods', async () => {
    const { log } = await import('../../src/services/logger.js');
    expect(typeof log.info).toBe('function');
    expect(typeof log.error).toBe('function');
    expect(typeof log.fatal).toBe('function');
  });
});
