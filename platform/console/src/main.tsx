import '@fontsource/ibm-plex-sans-kr/400.css';
import '@fontsource/ibm-plex-sans-kr/600.css';
import '@fontsource/ibm-plex-mono/400.css';
import '@fontsource/ibm-plex-mono/600.css';
import './index.css';
import { StrictMode, useEffect, useState, type ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Theme } from '@astryxdesign/core/theme';
import { neutralTheme } from '@astryxdesign/theme-neutral/built';
import { createHttpClient } from './api/client';
import { App } from './App';
import { loadConfig } from './config';
import { ConfigProvider } from './live/live';
import { ApiProvider } from './state';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: true, staleTime: 2_000 },
    mutations: { retry: 0 },
  },
});

type Mode = 'light' | 'dark' | 'system';
const docMode = (): Mode => {
  const t = document.documentElement.dataset.theme;
  return t === 'light' || t === 'dark' ? t : 'system';
};

/** Astryx follows the console's own light/dark switch (data-theme on <html>, set by the header). */
function AstryxTheme({ children }: { children: ReactNode }) {
  const [mode, setMode] = useState<Mode>(docMode);
  useEffect(() => {
    const o = new MutationObserver(() => setMode(docMode()));
    o.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    return () => o.disconnect();
  }, []);
  return (
    <Theme theme={neutralTheme} mode={mode}>
      {children}
    </Theme>
  );
}

const rootEl = document.getElementById('root');
if (!rootEl) throw new Error('#root가 없습니다');
const root = createRoot(rootEl);

loadConfig().then(
  (config) => {
    root.render(
      <StrictMode>
        <QueryClientProvider client={queryClient}>
          <ConfigProvider config={config}>
            <ApiProvider client={createHttpClient(config.apiBase)}>
              <AstryxTheme>
                <App />
              </AstryxTheme>
            </ApiProvider>
          </ConfigProvider>
        </QueryClientProvider>
      </StrictMode>,
    );
  },
  (err: unknown) => {
    root.render(
      <main className="welcome" role="alert">
        <h1>설정을 읽지 못했습니다</h1>
        <p>{err instanceof Error ? err.message : String(err)}</p>
        <p className="muted">
          컨테이너는 시작할 때 환경 변수 <code>BRAVO_API_BASE</code>로 <code>config.json</code>을 만듭니다. 개발 서버는 <code>VITE_API_BASE</code>를 씁니다.
        </p>
      </main>,
    );
  },
);
