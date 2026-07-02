import type { ReactNode } from 'react';
import { LabmateMark } from '../LabmateMark';
import type { Mode } from './ChatScreen';
import { welcomeCopyFor } from './newSessionContent';

export interface NewSessionWelcomeProps {
  mode: Mode;
  greeting: string;
  onStarter: (prompt: string) => void;
  composer: ReactNode;
}

export function NewSessionWelcome({ mode, greeting, onStarter, composer }: NewSessionWelcomeProps) {
  const { subtext, starters } = welcomeCopyFor(mode);
  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', minHeight: 0, padding: '24px 24px 60px' }}>
      <div className="lm-heroin" style={{ width: '100%', maxWidth: 640, display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
        {/* Logo badge — reuse the shared mark (tile variant = gradient tile + orbital SVG) */}
        <div style={{ marginBottom: 24 }}>
          <LabmateMark size={62} variant="tile" spin="slow" />
        </div>

        <div style={{ fontSize: 28, fontWeight: 600, letterSpacing: '-0.02em', color: '#f0f2f5', textAlign: 'center', marginBottom: 9 }}>
          {greeting}
        </div>
        <div style={{ fontSize: 15, lineHeight: 1.5, color: '#7e8693', textAlign: 'center', marginBottom: 30, maxWidth: 460 }}>
          {subtext}
        </div>

        {/* Centered composer — passed in as a slot from ChatScreen (renders a fresh Composer instance in each branch) */}
        <div style={{ width: '100%' }}>{composer}</div>

        {/* Starter chips */}
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 9, justifyContent: 'center', marginTop: 22 }}>
          {starters.map((st) => (
            <button
              key={st.label}
              type="button"
              data-testid="starter-chip"
              onClick={() => onStarter(st.prompt)}
              style={{ display: 'flex', alignItems: 'center', gap: 8, border: '1px solid #20242c', background: '#13161c', borderRadius: 9, padding: '9px 13px', cursor: 'pointer', fontSize: 13, color: '#c7ccd3' }}
            >
              <span style={{ fontSize: 13, opacity: 0.85 }}>{st.icon}</span>
              {st.label}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
