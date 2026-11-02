/**
 * App-wide state: the API client, the dev user (token), and the plan event stream.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { ApiClient, StreamStatus } from './api/client';
import type { DevToken, PlanEvent, Role } from './api/types';

const ApiContext = createContext<ApiClient | null>(null);

export function ApiProvider({ client, children }: { client: ApiClient; children: ReactNode }) {
  return <ApiContext.Provider value={client}>{children}</ApiContext.Provider>;
}

export function useApi(): ApiClient {
  const c = useContext(ApiContext);
  if (!c) throw new Error('ApiProvider가 없습니다');
  return c;
}

function readStorage(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}
function writeStorage(key: string, value: string | null): void {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    /* storage blocked: keep the value in memory only */
  }
}

/** Best-effort read of the JWT payload email. UI hint only; the API verifies the token. */
function emailFromJwt(token: string): string | null {
  const part = token.split('.')[1];
  if (!part) return null;
  try {
    const json = JSON.parse(atob(part.replace(/-/g, '+').replace(/_/g, '/'))) as unknown;
    if (typeof json === 'object' && json !== null && 'email' in json && typeof json.email === 'string') return json.email;
  } catch {
    return null;
  }
  return null;
}

export interface Session {
  tokens: DevToken[];
  tokensError: unknown;
  tokensLoading: boolean;
  user: DevToken | null;
  selectRole: (role: Role) => void;
  setPastedToken: (token: string) => void;
}

const ROLE_KEY = 'bravo.role';

export function useSession(): Session {
  const api = useApi();
  const tokensQ = useQuery({ queryKey: ['dev-tokens'], queryFn: () => api.getDevTokens(), staleTime: Infinity, retry: 1 });
  const [role, setRole] = useState<Role>(() => {
    const r = readStorage(ROLE_KEY);
    return r === 'approver' || r === 'viewer' ? r : 'operator';
  });
  const [pasted, setPasted] = useState<DevToken | null>(null);

  const tokens = useMemo(() => tokensQ.data ?? [], [tokensQ.data]);
  const user = pasted ?? tokens.find((t) => t.role === role) ?? tokens[0] ?? null;

  const selectRole = useCallback((r: Role) => {
    setPasted(null);
    setRole(r);
    writeStorage(ROLE_KEY, r);
  }, []);

  const setPastedToken = useCallback((token: string) => {
    const email = emailFromJwt(token) ?? '알 수 없음';
    const local = email.split('@')[0];
    const inferred: Role = local === 'operator' || local === 'approver' ? local : 'viewer';
    setPasted({ role: inferred, email, token });
  }, []);

  return {
    tokens,
    tokensError: tokensQ.error,
    tokensLoading: tokensQ.isLoading,
    user,
    selectRole,
    setPastedToken,
  };
}

export interface PlanStream {
  events: PlanEvent[];
  status: StreamStatus;
  detail: string | null;
  /** Number of events received; read by the plan query to mark where its snapshot starts. */
  countRef: { readonly current: number };
}

/** Subscribes to GET /v1/plans/{id}/events. Keeps events across token switches and resumes by Last-Event-ID. */
export function usePlanStream(planId: string | null, token: string | null): PlanStream {
  const api = useApi();
  const qc = useQueryClient();
  const [events, setEvents] = useState<PlanEvent[]>([]);
  const [status, setStatus] = useState<StreamStatus>('closed');
  const [detail, setDetail] = useState<string | null>(null);
  const seen = useRef(new Set<string>());
  const lastId = useRef<string | null>(null);
  const countRef = useRef(0);
  const planRef = useRef<string | null>(null);

  if (planRef.current !== planId) {
    // New plan: drop the old stream state synchronously before subscribing.
    planRef.current = planId;
    seen.current = new Set();
    lastId.current = null;
    countRef.current = 0;
  }

  useEffect(() => {
    setEvents([]);
  }, [planId]);

  useEffect(() => {
    if (!planId || !token) {
      setStatus('closed');
      return;
    }
    let timer: ReturnType<typeof setTimeout> | undefined;
    const unsubscribe = api.subscribe(token, planId, lastId.current, {
      onEvent(e) {
        if (seen.current.has(e.id)) return;
        seen.current.add(e.id);
        lastId.current = e.id;
        countRef.current += 1;
        setEvents((prev) => [...prev, e]);
        // Coalesce bursts (e.g. eight run.stage events) into one snapshot refetch.
        clearTimeout(timer);
        timer = setTimeout(() => void qc.invalidateQueries({ queryKey: ['plan', planId] }), 200);
      },
      onStatus(s, d) {
        setStatus(s);
        setDetail(d ?? null);
      },
    });
    return () => {
      clearTimeout(timer);
      unsubscribe();
    };
  }, [api, qc, planId, token]);

  return { events, status, detail, countRef };
}

/**
 * Plan ID from the path `<base>plans/<id>`, the form of `console_url` and `approve_url`
 * that the API and MCP `plan_get` return. `?plan=<id>` also works.
 */
function planFromLocation(): string | null {
  const base = import.meta.env.BASE_URL;
  const rest = location.pathname.startsWith(base) ? location.pathname.slice(base.length) : location.pathname;
  const m = /^plans\/([^/]+)\/?$/.exec(rest);
  if (m?.[1]) return decodeURIComponent(m[1]);
  return new URLSearchParams(location.search).get('plan');
}

export function usePlanParam(): [string | null, (id: string | null) => void] {
  const [planId, setPlanId] = useState<string | null>(planFromLocation);
  useEffect(() => {
    const onPop = () => setPlanId(planFromLocation());
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);
  const set = useCallback((id: string | null) => {
    const base = import.meta.env.BASE_URL;
    history.pushState(null, '', id ? `${base}plans/${encodeURIComponent(id)}` : base);
    setPlanId(id);
    if (id) {
      const recent = (readStorage('bravo.recentPlans') ?? '').split('\n').filter((x) => x && x !== id);
      writeStorage('bravo.recentPlans', [id, ...recent].slice(0, 8).join('\n'));
    }
  }, []);
  return [planId, set];
}

export function recentPlans(): string[] {
  return (readStorage('bravo.recentPlans') ?? '').split('\n').filter(Boolean);
}

export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return now;
}

export type Theme = 'light' | 'dark' | null;

export function useTheme(): [Theme, (t: Theme) => void] {
  const [theme, setTheme] = useState<Theme>(() => {
    const t = readStorage('bravo.theme');
    return t === 'light' || t === 'dark' ? t : null;
  });
  useEffect(() => {
    if (theme) document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
    writeStorage('bravo.theme', theme);
  }, [theme]);
  return [theme, setTheme];
}
