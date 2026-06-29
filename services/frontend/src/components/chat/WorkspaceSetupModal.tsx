/**
 * Post-login prompt for the default workspace ("current directory"), shown once
 * when no default has been chosen yet. The default seeds every new chat; chats
 * can add more directories afterward.
 */
export function WorkspaceSetupModal({ onChoose, onSkip }: { onChoose: () => void; onSkip: () => void }) {
  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(7,8,11,0.72)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 50,
      }}
    >
      <div
        style={{
          width: 460,
          maxWidth: '90vw',
          background: '#0f1115',
          border: '1px solid #20242c',
          borderRadius: 16,
          padding: '26px 26px 22px',
          boxShadow: '0 24px 60px rgba(0,0,0,0.5)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
          <span style={{ fontSize: 22 }}>📁</span>
          <span style={{ fontSize: 16, fontWeight: 600, color: '#e6e8ec' }}>Choose your default workspace</span>
        </div>
        <p style={{ fontSize: 13.5, lineHeight: 1.6, color: '#939ba7', margin: '0 0 20px' }}>
          Labmate reads and edits files on <b style={{ color: '#c7ccd3' }}>your machine</b> while the model runs on the
          server. Pick the folder you usually work in — it becomes the “current directory” every new chat starts from.
          You can add more directories to any chat later, or reference files with <code style={{ fontFamily: "'IBM Plex Mono'", color: '#a78bfa' }}>@</code>.
        </p>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <button
            type="button"
            onClick={onChoose}
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 8,
              background: '#6aa6ff',
              color: '#0a0c10',
              border: 'none',
              borderRadius: 9,
              padding: '10px 16px',
              fontFamily: 'inherit',
              fontSize: 13.5,
              fontWeight: 600,
              cursor: 'pointer',
            }}
          >
            Choose folder…
          </button>
          <button
            type="button"
            onClick={onSkip}
            style={{
              background: 'transparent',
              border: 'none',
              color: '#5e6671',
              fontFamily: 'inherit',
              fontSize: 13,
              cursor: 'pointer',
            }}
          >
            Skip for now
          </button>
        </div>
      </div>
    </div>
  );
}
