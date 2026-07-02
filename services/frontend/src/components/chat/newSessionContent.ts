import type { Mode } from './ChatScreen';

export type WelcomeStarter = { icon: string; label: string; prompt: string };

export function greetingFor(now: Date, name?: string): string {
  const h = now.getHours();
  const tod = h >= 5 && h < 12 ? 'morning' : h >= 12 && h < 17 ? 'afternoon' : 'evening';
  const base = `Good ${tod}`;
  return name ? `${base}, ${name}` : base;
}

const COPY: Record<Mode, { subtext: string; starters: WelcomeStarter[] }> = {
  code: {
    subtext: 'Start a coding session, or paste a spec to pick up a milestone.',
    starters: [
      { icon: '⌘', label: 'Scaffold a service', prompt: 'Scaffold a service' },
      { icon: '⌗', label: 'Map the repo', prompt: 'Map the repo' },
      { icon: '▣', label: 'Explain a diff', prompt: 'Explain a diff' },
    ],
  },
  paper: {
    subtext: 'Draft a section, tighten a claim, or paste an outline to begin.',
    starters: [
      { icon: '📄', label: 'Draft a Results section', prompt: 'Draft a Results section' },
      { icon: '✎', label: 'Tighten my abstract', prompt: 'Tighten my abstract' },
      { icon: '✓', label: 'Check citations vs data', prompt: 'Check citations vs data' },
    ],
  },
  chat: {
    subtext: 'Ask about the codebase, a serving flag, or a past decision.',
    starters: [
      { icon: '💬', label: 'Summarize a paper', prompt: 'Summarize a paper' },
      { icon: '⚡', label: 'Explain a serving flag', prompt: 'Explain a serving flag' },
      { icon: '◈', label: 'Recall a past decision', prompt: 'Recall a past decision' },
    ],
  },
};

export function welcomeCopyFor(mode: Mode): { subtext: string; starters: WelcomeStarter[] } {
  return COPY[mode];
}
