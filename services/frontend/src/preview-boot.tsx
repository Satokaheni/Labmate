import { createRoot } from 'react-dom/client';
import './styles/tokens.css';
import { BootScreen } from './screens/BootScreen';
import type { Subsystem } from './types/events';

/* Visual-check harness for the boot screen's regression-curve animation. */

const subsystems: Subsystem[] = [
  { id: 'brain', label: 'Brain', state: 'ready', message: 'llama.cpp :8000' },
  { id: 'nervous_system', label: 'Nervous system', state: 'ready', message: 'MCP bridge · 3 tools' },
  { id: 'hands', label: 'Hands', state: 'starting', message: 'loading skills…' },
  { id: 'memory', label: 'Memory', state: 'pending', message: 'queued' },
  { id: 'workspace', label: 'Workspace', state: 'pending', message: 'queued' },
];

createRoot(document.getElementById('root')!).render(<BootScreen subsystems={subsystems} onRetry={() => {}} />);
