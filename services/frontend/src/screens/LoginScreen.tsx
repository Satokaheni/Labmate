import { useState, type FormEvent } from 'react';
import { LabmateMark } from '@/components/LabmateMark';

export interface LoginCredentials {
  email: string;
  password: string;
  remember: boolean;
}

export interface LoginScreenProps {
  onSubmit: (creds: LoginCredentials) => void;
  submitting?: boolean;
  error?: string;
}

function errorMessage(error: string): string {
  if (error === 'invalid_credentials') return 'Invalid email or password.';
  if (error === 'empty_fields') return 'Email and password are required.';
  if (error === 'network_error') return 'Could not reach the server. Check your connection.';
  if (error === 'locked') return 'Too many failed attempts. Try again in 5 minutes.';
  return error;
}

export function LoginScreen({ onSubmit, submitting = false, error }: LoginScreenProps) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [remember, setRemember] = useState(false);
  const [showPw, setShowPw] = useState(false);

  const canSubmit = email.trim().length > 0 && password.length > 0 && !submitting;

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    onSubmit({ email: email.trim(), password, remember });
  };

  return (
    <div className="relative h-full w-full overflow-hidden bg-page-alt text-primary">
      {/* ambient radial gradients */}
      <div
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            'radial-gradient(700px 500px at 18% 12%, rgba(106,166,255,.10), transparent 60%), radial-gradient(700px 500px at 82% 88%, rgba(167,139,250,.10), transparent 60%)',
        }}
      />
      {/* grid texture */}
      <div
        className="pointer-events-none absolute inset-0 opacity-[0.04]"
        style={{
          backgroundImage:
            'linear-gradient(#fff 1px, transparent 1px), linear-gradient(90deg, #fff 1px, transparent 1px)',
          backgroundSize: '40px 40px',
        }}
      />

      {/* window chrome */}
      <div className="absolute left-5 top-5 flex gap-2">
        <span className="h-3 w-3 rounded-full" style={{ background: '#ff5f57' }} />
        <span className="h-3 w-3 rounded-full" style={{ background: '#febc2e' }} />
        <span className="h-3 w-3 rounded-full" style={{ background: '#28c840' }} />
      </div>
      <div className="absolute right-5 top-5 font-mono text-xs text-mono">labmate · desktop</div>

      {/* brand lockup */}
      <div className="absolute left-6 top-14 flex items-center gap-3">
        <LabmateMark size={36} variant="tile" breathe spin="slow" />
        <div className="flex flex-col leading-tight">
          <span className="font-sans text-lg font-semibold tracking-[-0.03em]">Labmate</span>
          <span className="font-mono text-[11px] text-mono">local research + coding copilot</span>
        </div>
      </div>

      {/* experiment graphic (bottom-left) — decorative training-run chart.
          Purely illustrative product texture: aria-hidden + pointer-events-none,
          and hidden on narrow windows so it never collides with the centered card. */}
      <div
        data-testid="experiment-graphic"
        aria-hidden="true"
        className="pointer-events-none absolute bottom-9 left-6 hidden w-[392px] max-w-[42vw] md:block"
      >
        <div
          className="mb-[15px] max-w-[340px] font-sans text-[17px] font-semibold leading-[1.38] tracking-[-0.01em] [text-wrap:balance]"
          style={{ color: '#cfd4da' }}
        >
          Run the experiment, read the diff, draft the paper — in one place.
        </div>

        {/* the chart itself is dimmed; headline + spec line stay full-strength */}
        <div style={{ opacity: 0.62 }}>
          <div className="mb-[7px] flex items-center">
            <span className="flex-1 font-mono text-[9.5px] uppercase tracking-[0.12em]" style={{ color: '#5e6671' }}>
              fig.1 — training run
            </span>
            <span className="flex items-center gap-3 font-mono text-[9.5px]" style={{ color: '#7e8693' }}>
              <span className="flex items-center gap-[5px]">
                <span className="inline-block h-[2px] w-[13px]" style={{ background: '#6aa6ff' }} />
                train
              </span>
              <span className="flex items-center gap-[5px]">
                <span className="inline-block w-[13px]" style={{ height: 0, borderTop: '2px dashed #a78bfa' }} />
                val
              </span>
            </span>
          </div>

          <svg viewBox="0 0 520 200" className="block h-auto max-h-[150px] w-full" role="presentation">
            <line x1="46" y1="14" x2="46" y2="186" stroke="#2a2f39" strokeWidth="1.5" />
            <line x1="46" y1="186" x2="506" y2="186" stroke="#2a2f39" strokeWidth="1.5" />
            <line x1="46" y1="60" x2="506" y2="60" stroke="#ffffff08" strokeWidth="1" />
            <line x1="46" y1="108" x2="506" y2="108" stroke="#ffffff08" strokeWidth="1" />
            <line x1="46" y1="148" x2="506" y2="148" stroke="#ffffff08" strokeWidth="1" />
            <text x="38" y="20" textAnchor="end" fontFamily="IBM Plex Mono" fontSize="9" fill="#4f5662">
              2.0
            </text>
            <text x="38" y="112" textAnchor="end" fontFamily="IBM Plex Mono" fontSize="9" fill="#4f5662">
              1.0
            </text>
            <text x="38" y="189" textAnchor="end" fontFamily="IBM Plex Mono" fontSize="9" fill="#4f5662">
              0.4
            </text>
            <polyline
              points="46,186 100,152 160,114 230,142 300,166 380,176 506,172"
              fill="none"
              stroke="#a78bfa"
              strokeWidth="2"
              strokeDasharray="4 5"
              opacity="0.7"
            />
            <polyline
              points="46,26 100,90 160,130 230,156 300,170 380,178 506,180"
              fill="none"
              stroke="#6aa6ff"
              strokeWidth="2.5"
              strokeLinejoin="round"
              strokeLinecap="round"
            />
            <circle cx="230" cy="156" r="3.5" fill="#0f1115" stroke="#6aa6ff" strokeWidth="2" />
            <circle cx="506" cy="180" r="3.5" fill="#0f1115" stroke="#6aa6ff" strokeWidth="2" />
          </svg>
        </div>

        <div className="mt-3 font-mono text-[10.5px] tracking-[0.02em]" style={{ color: '#5e6671' }}>
          gemma-4-12b · 12B · ctx 262,144 · 12 skills · build 0.1.0
        </div>
      </div>

      {/* sign-in card */}
      <div className="flex h-full items-center justify-center">
        <form
          onSubmit={handleSubmit}
          className="w-[360px] rounded-card border border-border-1 bg-panel p-7 shadow-card"
        >
          <h1 className="mb-5 text-base font-semibold">Sign in</h1>

          {error && (
            <div
              role="alert"
              className="mb-4 rounded-pill border border-[#7a2a2a] bg-[#2a1414] px-3 py-2 text-sm text-[#ff9b9b]"
            >
              {errorMessage(error)}
            </div>
          )}

          <label className="mb-1 block text-xs text-secondary" htmlFor="login-email">
            Email
          </label>
          <input
            id="login-email"
            type="email"
            autoComplete="email"
            disabled={submitting}
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="mb-4 w-full rounded-pill border border-border-2 bg-page px-3 py-2 text-sm outline-none focus:border-accent-purple disabled:opacity-50"
          />

          <label className="mb-1 block text-xs text-secondary" htmlFor="login-password">
            Password
          </label>
          <div className="relative mb-4">
            <input
              id="login-password"
              type={showPw ? 'text' : 'password'}
              autoComplete="current-password"
              disabled={submitting}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded-pill border border-border-2 bg-page px-3 py-2 pr-16 text-sm outline-none focus:border-accent-purple disabled:opacity-50"
            />
            <button
              type="button"
              onClick={() => setShowPw((s) => !s)}
              aria-label={showPw ? 'Hide password' : 'Show password'}
              className="absolute right-2 top-1/2 -translate-y-1/2 font-mono text-[11px] text-mono hover:text-secondary"
            >
              {showPw ? 'Hide' : 'Show'}
            </button>
          </div>

          <label className="mb-5 flex items-center gap-2 text-xs text-secondary">
            <input
              type="checkbox"
              checked={remember}
              disabled={submitting}
              onChange={(e) => setRemember(e.target.checked)}
            />
            Keep me signed in
          </label>

          <button
            type="submit"
            disabled={!canSubmit}
            className="flex w-full items-center justify-center gap-2 rounded-pill px-3 py-2 text-sm font-medium text-page transition disabled:cursor-not-allowed disabled:opacity-40"
            style={{ background: 'var(--accent-purple)' }}
          >
            {submitting ? (
              <>
                <span
                  data-testid="login-spinner"
                  className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-page border-t-transparent"
                />
                Authenticating…
              </>
            ) : (
              'Sign in'
            )}
          </button>
        </form>
      </div>

      {/* bottom status */}
      <div className="absolute bottom-5 left-1/2 flex -translate-x-1/2 items-center gap-2 font-mono text-[11px] text-mono">
        <span
          data-testid="health-dot"
          className="h-2 w-2 rounded-full [animation:pulse-dot_2s_ease-in-out_infinite] motion-reduce:!animate-none"
          style={{ background: 'var(--accent-green)' }}
        />
        local instance reachable · llama.cpp :8000
      </div>
    </div>
  );
}
