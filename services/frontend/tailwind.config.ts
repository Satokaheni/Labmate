import type { Config } from 'tailwindcss';

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        page: 'var(--surface-page)',
        'page-alt': 'var(--surface-page-alt)',
        panel: 'var(--surface-panel)',
        rail: 'var(--surface-rail)',
        'border-1': 'var(--border-1)',
        'border-2': 'var(--border-2)',
        'border-3': 'var(--border-3)',
        'border-4': 'var(--border-4)',
        primary: 'var(--text-primary)',
        'primary-alt': 'var(--text-primary-alt)',
        secondary: 'var(--text-secondary)',
        'text-mono': 'var(--text-mono)',
        muted: 'var(--text-muted)',
        'accent-blue': 'var(--accent-blue)',
        'accent-purple': 'var(--accent-purple)',
        'accent-green': 'var(--accent-green)',
        'accent-amber': 'var(--accent-amber)',
      },
      fontFamily: {
        sans: ['var(--font-sans)'],
        mono: ['var(--font-mono)'],
      },
      borderRadius: {
        card: 'var(--radius-card)',
        pill: 'var(--radius-pill)',
      },
      boxShadow: {
        card: 'var(--shadow-card)',
        tile: 'var(--shadow-tile)',
      },
      backgroundImage: {
        brand: 'var(--brand-grad)',
      },
    },
  },
  plugins: [],
} satisfies Config;
