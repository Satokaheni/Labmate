import { describe, it, expect } from 'vitest';
import { checkTestCommand } from './permissions';

describe('checkTestCommand', () => {
  it('allows pytest', () => {
    expect(checkTestCommand(['pytest', '-q'])).toBe('allow');
  });

  it('allows python', () => {
    expect(checkTestCommand(['python', '-m', 'pytest'])).toBe('allow');
  });

  it('allows python3', () => {
    expect(checkTestCommand(['python3', '-m', 'pytest'])).toBe('allow');
  });

  it('allows npm', () => {
    expect(checkTestCommand(['npm', 'test'])).toBe('allow');
  });

  it('allows npx', () => {
    expect(checkTestCommand(['npx', 'jest'])).toBe('allow');
  });

  it('allows yarn', () => {
    expect(checkTestCommand(['yarn', 'test'])).toBe('allow');
  });

  it('allows pnpm', () => {
    expect(checkTestCommand(['pnpm', 'test'])).toBe('allow');
  });

  it('allows jest', () => {
    expect(checkTestCommand(['jest'])).toBe('allow');
  });

  it('allows vitest', () => {
    expect(checkTestCommand(['vitest', 'run'])).toBe('allow');
  });

  it('allows go', () => {
    expect(checkTestCommand(['go', 'test'])).toBe('allow');
  });

  it('allows cargo', () => {
    expect(checkTestCommand(['cargo', 'test'])).toBe('allow');
  });

  it('allows make', () => {
    expect(checkTestCommand(['make', 'test'])).toBe('allow');
  });

  it('allows bun', () => {
    expect(checkTestCommand(['bun', 'test'])).toBe('allow');
  });

  it('allows deno', () => {
    expect(checkTestCommand(['deno', 'test'])).toBe('allow');
  });

  it('denies rm', () => {
    expect(checkTestCommand(['rm', '-rf', '/'])).toBe('deny');
  });

  it('denies dangerous shell commands', () => {
    expect(checkTestCommand(['sh', '-c', 'rm -rf /'])).toBe('deny');
  });

  it('denies absolute path to disallowed binary', () => {
    expect(checkTestCommand(['/bin/rm'])).toBe('deny');
  });

  it('allows absolute path to allowed binary (extracts basename)', () => {
    expect(checkTestCommand(['/opt/homebrew/bin/pytest'])).toBe('allow');
  });

  it('allows Windows executable extension on allowed binary', () => {
    expect(checkTestCommand(['pytest.exe'])).toBe('allow');
  });

  it('denies Windows executable extension on disallowed binary', () => {
    expect(checkTestCommand(['rm.exe'])).toBe('deny');
  });

  it('denies empty argv', () => {
    expect(checkTestCommand([])).toBe('deny');
  });

  it('denies when argv[0] is empty string', () => {
    expect(checkTestCommand([''])).toBe('deny');
  });

  it('is case-insensitive', () => {
    expect(checkTestCommand(['PYTEST'])).toBe('allow');
    expect(checkTestCommand(['PyTest'])).toBe('allow');
  });
});
