// CRITICAL: destination fd 2 = stderr. stdout carries JSON-RPC only.
import pino from 'pino';

export const log = pino(
  { level: process.env.LOG_LEVEL ?? 'info' },
  pino.destination(2),
);
