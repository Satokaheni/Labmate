import { useEffect, useRef, useState } from 'react';

/**
 * Returns `true` once `ms` has elapsed since `active` FIRST became true.
 *
 * Once armed, the timer runs to completion regardless of `active` later flipping
 * back to false — so a loader that finishes faster than `ms` still honors the
 * minimum display time (avoids a sub-second flash of the loading animation).
 *
 * Used by the boot/login screen to guarantee a minimum visible duration.
 */
export function useMinimumElapsed(active: boolean, ms: number): boolean {
  const [elapsed, setElapsed] = useState(false);
  const armed = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (active && !armed.current) {
      armed.current = true;
      timer.current = setTimeout(() => setElapsed(true), ms);
    }
    // No cleanup here: the timer must survive `active` flipping false so a
    // fast finish still honors the minimum. Cleared only on unmount (below).
  }, [active, ms]);

  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );

  return elapsed;
}
