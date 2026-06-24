import '@testing-library/jest-dom/vitest';

// Node.js 26 exposes an experimental (undefined) `localStorage` global that
// shadows jsdom's implementation. Delete both storage globals so jsdom can
// inject its own properly-scoped implementations.
if (typeof localStorage === 'undefined' || localStorage === null) {
  class MockStorage {
    private store: Record<string, string> = {};
    clear() { this.store = {}; }
    getItem(key: string) { return Object.prototype.hasOwnProperty.call(this.store, key) ? this.store[key] : null; }
    setItem(key: string, value: string) { this.store[key] = String(value); }
    removeItem(key: string) { delete this.store[key]; }
    key(n: number) { return Object.keys(this.store)[n] ?? null; }
    get length() { return Object.keys(this.store).length; }
  }
  Object.defineProperty(globalThis, 'localStorage', { value: new MockStorage(), writable: true });
  Object.defineProperty(globalThis, 'sessionStorage', { value: new MockStorage(), writable: true });
}
