import { describe, it, expect } from 'vitest';
import { capabilitiesFrame, CLIENT_CAPABILITIES } from './capabilities';

describe('capabilities', () => {
  it('capabilitiesFrame returns a frame with type client.capabilities and five builtin tools', () => {
    const frame = capabilitiesFrame();
    expect(frame.type).toBe('client.capabilities');
    expect(frame.protocolVersion).toBe(1);
    expect(frame.tools).toHaveLength(5);
  });

  it('capabilitiesFrame tool names are exactly read_file, write_file, list_dir, search_files, run_tests', () => {
    const frame = capabilitiesFrame();
    expect(frame.tools.map((t) => t.name)).toEqual([
      'read_file',
      'write_file',
      'list_dir',
      'search_files',
      'run_tests',
    ]);
  });

  it('capabilitiesFrame includes search_files with builtin source', () => {
    const frame = capabilitiesFrame();
    const searchFilesTool = frame.tools.find((t) => t.name === 'search_files');
    expect(searchFilesTool).toBeDefined();
    expect(searchFilesTool?.source).toBe('builtin');
  });

  it('capabilitiesFrame includes run_tests with builtin source', () => {
    const frame = capabilitiesFrame();
    const runTestsTool = frame.tools.find((t) => t.name === 'run_tests');
    expect(runTestsTool).toBeDefined();
    expect(runTestsTool?.source).toBe('builtin');
  });

  it('capabilitiesFrame tools all have source builtin', () => {
    const frame = capabilitiesFrame();
    expect(frame.tools.every((t) => t.source === 'builtin')).toBe(true);
  });

  it('CLIENT_CAPABILITIES matches capabilitiesFrame content (minus type)', () => {
    const frame = capabilitiesFrame();
    const { type, ...frameRest } = frame;
    expect(frameRest).toEqual(CLIENT_CAPABILITIES);
  });
});
