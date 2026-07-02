import { describe, expect, it } from 'vitest';
import { greetingFor, welcomeCopyFor } from './newSessionContent';

describe('greetingFor', () => {
  it('picks the time-of-day word by hour', () => {
    expect(greetingFor(new Date('2026-07-01T09:00:00'))).toBe('Good morning');
    expect(greetingFor(new Date('2026-07-01T14:00:00'))).toBe('Good afternoon');
    expect(greetingFor(new Date('2026-07-01T20:00:00'))).toBe('Good evening');
    expect(greetingFor(new Date('2026-07-01T03:00:00'))).toBe('Good evening'); // pre-dawn = evening
  });
  it('appends a name only when given', () => {
    expect(greetingFor(new Date('2026-07-01T09:00:00'), 'Jordan')).toBe('Good morning, Jordan');
    expect(greetingFor(new Date('2026-07-01T09:00:00'), '')).toBe('Good morning');
  });
});

describe('welcomeCopyFor', () => {
  it('returns mode-specific subtext and exactly three starters', () => {
    for (const mode of ['chat', 'paper', 'code'] as const) {
      const c = welcomeCopyFor(mode);
      expect(c.subtext.length).toBeGreaterThan(0);
      expect(c.starters).toHaveLength(3);
      for (const s of c.starters) {
        expect(s.icon).toBeTruthy();
        expect(s.label).toBeTruthy();
        expect(s.prompt).toBeTruthy();
      }
    }
  });
  it('uses the code starters for code mode', () => {
    expect(welcomeCopyFor('code').starters.map((s) => s.label)).toEqual([
      'Scaffold a service', 'Map the repo', 'Explain a diff',
    ]);
  });
});
