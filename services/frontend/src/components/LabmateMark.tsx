import { useId } from 'react';

export type OrbitVariant = 'tile' | 'onDark';
export type OrbitSpin = 'fast' | 'slow' | 'none';

export interface LabmateMarkProps {
  /** Outer tile side length in px. The inner mark is ~0.64x this. */
  size: number;
  variant?: OrbitVariant;
  spin?: OrbitSpin;
  breathe?: boolean;
  className?: string;
}

const SPIN_CLASS: Record<OrbitSpin, string> = {
  fast: 'orbit-spin-fast',
  slow: 'orbit-spin-slow',
  none: 'orbit-spin-none',
};

export function LabmateMark({
  size,
  variant = 'tile',
  spin = 'none',
  breathe = false,
  className,
}: LabmateMarkProps) {
  const gradId = useId();
  const markSize = Math.round(size * 0.64);

  const ringStroke = variant === 'tile' ? '#0a0c1055' : '#ffffff22';
  const primaryFill = variant === 'tile' ? '#0a0c10' : `url(#${gradId})`;
  const primaryOpacity = variant === 'tile' ? '0.92' : '1';
  const companionFill = variant === 'tile' ? '#0a0c10' : '#a78bfa';
  const companionOpacity = variant === 'tile' ? '0.92' : '1';

  const svg = (
    <svg
      width={markSize}
      height={markSize}
      viewBox="0 0 64 64"
      role="img"
      aria-label="Labmate"
      data-testid="orbit-mark"
    >
      {variant === 'onDark' && (
        <defs>
          <linearGradient id={gradId} x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor="#6aa6ff" />
            <stop offset="100%" stopColor="#a78bfa" />
          </linearGradient>
        </defs>
      )}
      <circle
        cx="32"
        cy="32"
        r="20"
        fill="none"
        stroke={ringStroke}
        strokeWidth="2.4"
        data-testid="orbit-ring"
      />
      <circle
        cx="26"
        cy="38"
        r="8"
        fill={primaryFill}
        opacity={primaryOpacity}
        data-testid="orbit-primary"
      />
      <g
        className={SPIN_CLASS[spin]}
        style={{ transformBox: 'view-box', transformOrigin: '32px 32px' }}
        data-testid="orbit-companion-group"
      >
        <circle
          cx="48"
          cy="18"
          r="4.8"
          fill={companionFill}
          opacity={companionOpacity}
          data-testid="orbit-companion"
        />
      </g>
    </svg>
  );

  if (variant === 'onDark') {
    return (
      <span
        className={['inline-flex items-center justify-center', breathe ? 'orbit-breathe' : '', className]
          .filter(Boolean)
          .join(' ')}
        style={{ width: size, height: size }}
      >
        {svg}
      </span>
    );
  }

  return (
    <span
      data-testid="orbit-tile"
      className={['inline-flex items-center justify-center shadow-tile', breathe ? 'orbit-breathe' : '', className]
        .filter(Boolean)
        .join(' ')}
      style={{
        width: `${size}px`,
        height: `${size}px`,
        borderRadius: `${Math.round(size * 0.27)}px`,
        background: 'linear-gradient(140deg, #6aa6ff, #a78bfa)',
      }}
    >
      {svg}
    </span>
  );
}
