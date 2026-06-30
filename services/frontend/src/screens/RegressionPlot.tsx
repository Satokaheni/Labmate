import { useEffect, useRef } from 'react';
import { seededScatter } from '@/lib/scatter';

export interface RegressionPlotProps {
  progress: number;
  seed?: number;
  count?: number;
}

const W = 400;
const H = 240;
const FIT_MS = 2400; // time to converge from under-fit to fitted

/** ease-in-out cubic — a gradual, even convergence so the fit is visible
 *  across the whole duration rather than snapping early. */
function easeInOutCubic(t: number): number {
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
}

/** Build the logistic-curve path `d` for a given elapsed time (0 = under-fit). */
function curvePath(elapsed: number, progress: number): string {
  const fit = easeInOutCubic(Math.min(1, elapsed / FIT_MS));
  const settle = elapsed > FIT_MS ? Math.sin((elapsed - FIT_MS) / 650) * 0.5 : 0;
  const kEnd = 9 + progress * 6;
  const k = 1.2 + (kEnd - 1.2) * fit + settle;
  const x0 = 0.33 + (0.5 - 0.33) * fit;

  let d = '';
  for (let i = 0; i <= 60; i++) {
    const x = i / 60;
    const y = 1 / (1 + Math.exp(-(x - x0) * k));
    d += `${i === 0 ? 'M' : 'L'} ${x * W} ${H - y * H} `;
  }
  return d.trim();
}

export function RegressionPlot({ progress, seed = 1337, count = 24 }: RegressionPlotProps) {
  const pts = seededScatter(seed, count);
  const pathRef = useRef<SVGPathElement>(null);

  // Animate the *shape* of the curve (under-fit → fitted, then a slight settle)
  // by mutating the path `d` per frame through the ref — no React state, so the
  // component does not re-render 60×/sec.
  useEffect(() => {
    const apply = (elapsed: number) => {
      pathRef.current?.setAttribute('d', curvePath(elapsed, progress));
    };
    apply(0);
    if (typeof requestAnimationFrame !== 'function') return; // non-animating env (tests)
    let raf = 0;
    let start: number | null = null;
    const loop = (ts: number) => {
      if (start === null) start = ts;
      apply(ts - start);
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [progress]);

  return (
    <svg
      width="100%"
      height="100%"
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="xMidYMid slice"
      data-testid="regression-plot"
      aria-hidden="true"
    >
      {/* initial frame is the under-fit curve; the effect animates it from there */}
      <path ref={pathRef} d={curvePath(0, progress)} fill="none" stroke="#6aa6ff" strokeWidth="1.5" opacity={0.55} />
      {pts.map((p) => (
        <circle
          key={`${p.cls}-${p.x.toFixed(4)}-${p.y.toFixed(4)}`}
          data-testid="scatter-point"
          cx={p.x * W}
          cy={H - p.y * H}
          r="3"
          fill={p.cls === 1 ? '#a78bfa' : '#6aa6ff'}
          opacity="0.5"
        />
      ))}
    </svg>
  );
}
