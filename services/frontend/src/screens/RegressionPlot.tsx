function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export interface ScatterPoint {
  x: number;
  y: number;
  cls: 0 | 1;
}

export function seededScatter(seed: number, count: number): ScatterPoint[] {
  const rnd = mulberry32(seed);
  const pts: ScatterPoint[] = [];
  for (let i = 0; i < count; i++) {
    const x = rnd();
    const noise = (rnd() - 0.5) * 0.4;
    const boundary = 1 / (1 + Math.exp(-(x - 0.5) * 8));
    const y = Math.min(1, Math.max(0, boundary + noise));
    const cls: 0 | 1 = y > boundary ? 1 : 0;
    pts.push({ x, y, cls });
  }
  return pts;
}

export interface RegressionPlotProps {
  progress: number;
  seed?: number;
  count?: number;
}

export function RegressionPlot({ progress, seed = 1337, count = 24 }: RegressionPlotProps) {
  const pts = seededScatter(seed, count);
  const W = 400;
  const H = 240;
  const k = 4 + progress * 10;
  const curve: string[] = [];
  for (let i = 0; i <= 60; i++) {
    const x = i / 60;
    const y = 1 / (1 + Math.exp(-(x - 0.5) * k));
    curve.push(`${i === 0 ? 'M' : 'L'} ${x * W} ${H - y * H}`);
  }

  return (
    <svg
      width="100%"
      height="100%"
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="xMidYMid slice"
      data-testid="regression-plot"
      aria-hidden="true"
    >
      <path d={curve.join(' ')} fill="none" stroke="#6aa6ff" strokeWidth="1.5" opacity={0.25 + progress * 0.5} />
      {pts.map((p, i) => (
        <circle
          key={i}
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
