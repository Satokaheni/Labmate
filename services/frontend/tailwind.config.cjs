/** @type {import('tailwindcss').Config} */
// Maps the design-token CSS variables (src/styles/tokens.css) to the Tailwind
// utility names the components use (text-primary, bg-panel, border-border-1,
// rounded-pill, font-mono, ...). Without this config the @tailwind directives
// don't compile and every component class is a no-op (unstyled UI).
module.exports = {
  content: ['./index.html', './src/**/*.{ts,tsx,js,jsx}'],
  theme: {
    extend: {
      colors: {
        primary: 'var(--text-primary)',
        'primary-alt': 'var(--text-primary-alt)',
        secondary: 'var(--text-secondary)',
        'secondary-hover': '#a8b0bc',
        mono: 'var(--text-mono)',
        muted: 'var(--text-muted)',
        page: 'var(--surface-page)',
        'page-alt': 'var(--surface-page-alt)',
        panel: 'var(--surface-panel)',
        rail: 'var(--surface-rail)',
        'rail-hover': '#161a21',
        'border-1': 'var(--border-1)',
        'border-2': 'var(--border-2)',
        'border-3': 'var(--border-3)',
        'border-4': 'var(--border-4)',
        'accent-blue': 'var(--accent-blue)',
        'accent-purple': 'var(--accent-purple)',
        'accent-green': 'var(--accent-green)',
        'accent-amber': 'var(--accent-amber)',
      },
      borderRadius: {
        pill: 'var(--radius-pill)',
        card: 'var(--radius-card)',
      },
      fontFamily: {
        sans: ['IBM Plex Sans', 'system-ui', 'sans-serif'],
        mono: ['IBM Plex Mono', 'ui-monospace', 'monospace'],
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
};
