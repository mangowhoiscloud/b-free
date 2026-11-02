/**
 * Live data layer for the watch screens. Every read is a GET through the console's own
 * read-only proxy (/svc/<name>/) or the API. A failed read is never replaced by sample data:
 * the screen shows "연결 안 됨" with the endpoint it tried and why.
 */
import { createContext, useContext, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Banner } from '@astryxdesign/core/Banner';
import { Badge, type BadgeVariant } from '@astryxdesign/core/Badge';
import { StatusDot } from '@astryxdesign/core/StatusDot';
import { Text } from '@astryxdesign/core/Text';
import type { RuntimeConfig } from '../config';

const ConfigContext = createContext<RuntimeConfig | null>(null);

export function ConfigProvider({ config, children }: { config: RuntimeConfig; children: ReactNode }) {
  return <ConfigContext.Provider value={config}>{children}</ConfigContext.Provider>;
}

export function useConfig(): RuntimeConfig {
  const c = useContext(ConfigContext);
  if (!c) throw new Error('ConfigProvider가 없습니다');
  return c;
}

export type Service = 'collector' | 'gate' | 'executor' | 'prometheus' | 'app-src' | 'app-dst';

export class LiveError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = 'LiveError';
    this.status = status;
  }
}

/** Plain Korean reason for a failed live read. */
export function liveReason(err: unknown): string {
  if (err instanceof LiveError) {
    if (err.status === 0) return `서비스에 닿지 못함 (${err.message})`;
    if (err.status === 502 || err.status === 504) return `서비스가 응답하지 않음 (${err.status}, 컨테이너가 꺼져 있거나 이름을 찾지 못함)`;
    if (err.status === 404) return `엔드포인트 없음 (404)`;
    return `${err.message} (${err.status})`;
  }
  return err instanceof Error ? err.message : '알 수 없는 오류';
}

export async function getJson(url: string, timeoutMs = 8000): Promise<unknown> {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  let res: Response;
  try {
    res = await fetch(url, { headers: { Accept: 'application/json' }, credentials: 'omit', signal: ctrl.signal });
  } catch (e) {
    throw new LiveError(0, ctrl.signal.aborted ? `${timeoutMs / 1000}초 안에 응답 없음` : e instanceof Error ? e.message : String(e));
  } finally {
    clearTimeout(t);
  }
  const text = await res.text();
  if (!res.ok) {
    let msg = res.statusText || '요청 실패';
    try {
      const o = JSON.parse(text) as { detail?: unknown; error?: unknown };
      if (typeof o.detail === 'string') msg = o.detail;
      else if (typeof o.error === 'string') msg = o.error;
    } catch {
      /* not JSON */
    }
    throw new LiveError(res.status, msg);
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new LiveError(res.status, 'JSON이 아닌 응답');
  }
}

export interface Live<T> {
  data: T | undefined;
  error: unknown;
  endpoint: string;
  updatedAt: number;
  isLoading: boolean;
}

/** Polls a GET endpoint of a proxied pilot service. `path` has no leading slash. */
export function useLive<T>(service: Service, path: string, intervalMs: number, parse: (v: unknown) => T): Live<T> {
  const { svcBase } = useConfig();
  const url = `${svcBase}/${service}/${path}`;
  const q = useQuery({
    queryKey: ['live', url],
    queryFn: async () => parse(await getJson(url)),
    refetchInterval: intervalMs,
    refetchIntervalInBackground: false,
    retry: 0,
    staleTime: 0,
  });
  return { data: q.data, error: q.error, endpoint: `GET /svc/${service}/${path}`, updatedAt: q.dataUpdatedAt, isLoading: q.isLoading };
}

/** Explicit "not connected" state: what was tried and why it failed. */
export function NotConnected({ title, endpoint, reason }: { title?: string; endpoint: string; reason: string }) {
  return (
    <Banner
      status="warning"
      container="card"
      title={title ?? '연결 안 됨'}
      description={
        <span>
          {reason} · <code className="mono">{endpoint}</code>
        </span>
      }
    />
  );
}

/** Loading / error / data switch for one live read. */
export function LiveGate<T>({ live, children, title }: { live: Live<T>; children: (d: T) => ReactNode; title?: string }) {
  if (live.data !== undefined && !live.error) return <>{children(live.data)}</>;
  if (live.error) return <NotConnected title={title} endpoint={live.endpoint} reason={liveReason(live.error)} />;
  return (
    <Text type="supporting" color="secondary">
      불러오는 중… <span className="mono">{live.endpoint}</span>
    </Text>
  );
}

export function Fresh({ at, label = '갱신' }: { at: number; label?: string }) {
  if (!at) return null;
  return (
    <Text type="supporting" color="secondary">
      {label} {new Date(at).toLocaleTimeString('ko-KR', { hour12: false })}
    </Text>
  );
}

export function OkDot({ ok, label }: { ok: boolean | null; label: string }) {
  return <StatusDot variant={ok === null ? 'neutral' : ok ? 'success' : 'error'} label={label} />;
}

export function Tag({ variant, label }: { variant: BadgeVariant; label: ReactNode }) {
  return <Badge variant={variant} label={label} />;
}

export const asRecord = (v: unknown): Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
export const asArray = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
export const str = (v: unknown): string | null => (typeof v === 'string' ? v : null);
export const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);
