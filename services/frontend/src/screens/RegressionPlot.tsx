import { seededScatter } from '@/lib/scatter';

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
