export type RightView = 'skills' | 'files' | null;

const KEY = 'lm.rightView';

/** Read the persisted right-column view. Defaults to null (hidden) when unset/invalid/unavailable. */
export function readRightView(): RightView {
  try {
    const v = localStorage.getItem(KEY);
    return v === 'skills' || v === 'files' ? v : null;
  } catch {
    return null;
  }
}

/** Persist the right-column view; removing the key when hidden. try/catch-guarded (jsdom/privacy-safe). */
export function writeRightView(v: RightView): void {
  try {
    if (v === null) localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, v);
  } catch {
    /* ignore */
  }
}
