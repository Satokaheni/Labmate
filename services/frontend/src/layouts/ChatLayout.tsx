import type { ReactNode } from 'react';

export interface ChatLayoutProps {
  topBar: ReactNode;
  left: ReactNode;
  center: ReactNode;
  right?: ReactNode;
}

export function ChatLayout({ topBar, left, center, right }: ChatLayoutProps) {
  return (
    <div className="flex h-full w-full flex-col bg-page text-primary">
      <div data-testid="layout-topbar" className="flex h-12 shrink-0 items-center border-b border-border-1 px-4">
        {topBar}
      </div>
      <div className={`grid min-h-0 flex-1 ${right ? 'grid-cols-[260px_minmax(0,1fr)_360px]' : 'grid-cols-[260px_minmax(0,1fr)]'}`}>
        <aside data-testid="layout-left" className="flex min-h-0 flex-col border-r border-border-1 bg-rail">
          {left}
        </aside>
        <main data-testid="layout-center" className="flex min-h-0 flex-col">
          {center}
        </main>
        {right && (
          <aside data-testid="layout-right" className="flex min-h-0 flex-col border-l border-border-1 bg-page-alt">
            {right}
          </aside>
        )}
      </div>
    </div>
  );
}
