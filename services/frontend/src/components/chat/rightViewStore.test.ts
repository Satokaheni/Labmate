import { afterEach, describe, expect, it } from 'vitest';
import { readRightView, writeRightView } from './rightViewStore';

afterEach(() => {
  try { localStorage.removeItem('lm.rightView'); } catch { /* ignore */ }
});

describe('rightViewStore', () => {
  it('defaults to hidden (null) when nothing is stored', () => {
    expect(readRightView()).toBeNull();
  });

  it('round-trips skills and files', () => {
    writeRightView('skills');
    expect(localStorage.getItem('lm.rightView')).toBe('skills');
    expect(readRightView()).toBe('skills');

    writeRightView('files');
    expect(readRightView()).toBe('files');
  });

  it('writing null clears the stored value (back to hidden)', () => {
    writeRightView('skills');
    writeRightView(null);
    expect(localStorage.getItem('lm.rightView')).toBeNull();
    expect(readRightView()).toBeNull();
  });

  it('ignores an invalid stored value', () => {
    try { localStorage.setItem('lm.rightView', 'garbage'); } catch { /* ignore */ }
    expect(readRightView()).toBeNull();
  });
});
