import { useEffect, useRef, useState } from 'react';
import { LabmateMark } from '@/components/LabmateMark';

export interface OnboardingScreenProps {
  onSaved: (wsUrl: string) => void;
}

type TestState = 'idle' | 'testing' | 'ok' | 'fail';

function normaliseUrl(raw: string): string {
  const s = raw.trim();
  // Accept https:// or http:// and convert to wss:// / ws://
  if (s.startsWith('https://')) return s.replace(/^https:\/\//, 'wss://').replace(/\/?$/, '/ws');
  if (s.startsWith('http://'))  return s.replace(/^http:\/\//, 'ws://').replace(/\/?$/, '/ws');
  // Already ws/wss — just ensure /ws suffix
  if (s.startsWith('ws://') || s.startsWith('wss://')) {
    return s.endsWith('/ws') ? s : s.replace(/\/?$/, '/ws');
  }
  return s;
}

export function OnboardingScreen({ onSaved }: OnboardingScreenProps) {
  const [raw, setRaw] = useState('');
  const [testState, setTestState] = useState<TestState>('idle');
  const [errorMsg, setErrorMsg] = useState('');
  const [saving, setSaving] = useState(false);

  const testWsRef = useRef<WebSocket | null>(null);
  const testTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    return () => {
      mountedRef.current = false;
      if (testTimerRef.current) clearTimeout(testTimerRef.current);
      if (testWsRef.current) testWsRef.current.close();
    };
  }, []);

  const url = normaliseUrl(raw);
  const valid = url.startsWith('ws://') || url.startsWith('wss://');

  const testConnection = () => {
    if (!valid) return;
    if (testTimerRef.current) clearTimeout(testTimerRef.current);
    if (testWsRef.current) testWsRef.current.close();
    setTestState('testing');
    setErrorMsg('');
    const ws = new WebSocket(url);
    testWsRef.current = ws;
    const timer = setTimeout(() => {
      ws.close();
      testWsRef.current = null;
      if (mountedRef.current) {
        setTestState('fail');
        setErrorMsg('Timed out — is the backend running?');
      }
    }, 6000);
    testTimerRef.current = timer;
    ws.onopen = () => {
      clearTimeout(timer);
      testTimerRef.current = null;
      ws.onopen = null;
      ws.onerror = null;
      ws.close();
      testWsRef.current = null;
      if (mountedRef.current) setTestState('ok');
    };
    ws.onerror = () => {
      clearTimeout(timer);
      testTimerRef.current = null;
      testWsRef.current = null;
      if (mountedRef.current) {
        setTestState('fail');
        setErrorMsg('Could not connect. Check the URL and that the backend is up.');
      }
    };
  };

  const save = async () => {
    if (!valid) return;
    setSaving(true);
    await window.electronAPI?.setConfig(url);
    if (mountedRef.current) onSaved(url);
  };

  return (
    <div className="flex h-full w-full items-center justify-center bg-page">
      <div className="flex w-full max-w-md flex-col gap-6 p-8">
        <div className="flex flex-col items-center gap-3">
          <LabmateMark size={48} variant="tile" breathe />
          <h1 className="text-xl font-semibold text-primary">Connect Labmate</h1>
          <p className="text-center text-sm text-mono">
            Enter the WebSocket URL of your backend.
            <br />
            Looks like <span className="font-mono text-xs" style={{ color: 'var(--accent-blue)' }}>wss://your-pod.runpod.net/ws</span>
          </p>
        </div>

        <div className="flex flex-col gap-2">
          <input
            type="text"
            value={raw}
            onChange={(e) => { setRaw(e.target.value); setTestState('idle'); setErrorMsg(''); }}
            placeholder="wss://…  or  https://…"
            className="w-full rounded-card border border-border-2 bg-panel px-3 py-2 text-sm text-primary outline-none placeholder:text-mono focus:border-[var(--accent-blue)]"
            onKeyDown={(e) => e.key === 'Enter' && void save()}
            autoFocus
          />
          {valid && raw !== url && (
            <p className="font-mono text-[11px] text-mono">→ {url}</p>
          )}
          {errorMsg && (
            <p className="text-[12px]" style={{ color: '#ff6b6b' }}>{errorMsg}</p>
          )}
          {testState === 'ok' && (
            <p className="text-[12px]" style={{ color: '#4ade80' }}>Connected successfully.</p>
          )}
        </div>

        <div className="flex gap-2">
          <button
            type="button"
            onClick={testConnection}
            disabled={!valid || testState === 'testing'}
            className="flex-1 rounded-pill border border-border-2 py-2 text-sm text-mono disabled:opacity-40"
          >
            {testState === 'testing' ? 'Testing…' : 'Test connection'}
          </button>
          <button
            type="button"
            onClick={() => void save()}
            disabled={!valid || saving}
            className="flex-1 rounded-pill py-2 text-sm font-medium text-page disabled:opacity-40"
            style={{ background: 'var(--accent-blue)' }}
          >
            {saving ? 'Saving…' : 'Connect'}
          </button>
        </div>
      </div>
    </div>
  );
}
