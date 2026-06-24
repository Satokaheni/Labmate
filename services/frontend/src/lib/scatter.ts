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
