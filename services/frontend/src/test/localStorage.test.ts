import { describe, it, expect, beforeEach } from 'vitest';

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
});

describe('localStorage test', () => {
  it('should work with localStorage', () => {
    localStorage.setItem('test', 'value');
    expect(localStorage.getItem('test')).toBe('value');
  });
  it('should work with sessionStorage', () => {
    sessionStorage.setItem('test2', 'val2');
    expect(sessionStorage.getItem('test2')).toBe('val2');
  });
});
