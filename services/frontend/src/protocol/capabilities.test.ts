import { describe, it, expect } from 'vitest';
import { capabilitiesFrame, CLIENT_CAPABILITIES } from './capabilities';

describe('capabilities', () => {
  it('capabilitiesFrame returns a frame with type client.capabilities and three builtin tools', () => {
    const frame = capabilitiesFrame();
    expect(frame.type).toBe('client.capabilities');
    expect(frame.protocolVersion).toBe(1);
    expect(frame.tools).toHaveLength(3);
  });

  it('capabilitiesFrame tool names are exactly read_file, write_file, list_dir', () => {
    const frame = capabilitiesFrame();
    expect(frame.tools.map((t) => t.name)).toEqual(['read_file', 'write_file', 'list_dir']);
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
