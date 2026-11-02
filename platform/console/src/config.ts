/**
 * Runtime configuration. Built images contain no API address: the container writes
 * config.json at start from BRAVO_API_BASE (and optional observability links), so one
 * image runs in any environment. The Vite dev server may use VITE_API_BASE instead.
 */
export interface RuntimeConfig {
  apiBase: string;
  /** Same-origin read-only proxy root for pilot services (/svc/<name>/). */
  svcBase: string;
  grafanaUrl: string | null;
  prometheusUrl: string | null;
  tempoUrl: string | null;
  publicFrontHost: string;
  /** Public DNS-over-HTTPS JSON resolver for the edge-name check; null = not shown. */
  dohUrl: string | null;
}

export class ConfigError extends Error {
  override name = 'ConfigError';
}

function validBase(v: unknown, source: string): string {
  if (typeof v !== 'string' || v.trim() === '') throw new ConfigError(`${source}에 API 주소가 없습니다.`);
  let url: URL;
  try {
    url = new URL(v.trim());
  } catch {
    throw new ConfigError(`${source}의 API 주소가 URL이 아닙니다: ${v}`);
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new ConfigError(`${source}의 API 주소는 http 또는 https여야 합니다: ${v}`);
  }
  return url.href.replace(/\/+$/, '');
}

/** Optional link: empty or invalid -> null (the screen then says the link is not set). */
function optionalUrl(v: unknown): string | null {
  if (typeof v !== 'string' || v.trim() === '') return null;
  try {
    const url = new URL(v.trim());
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href.replace(/\/+$/, '') : null;
  } catch {
    return null;
  }
}

const SVC_BASE = `${import.meta.env.BASE_URL}svc`;
const DEFAULT_FRONT = 'b-free.mangowhois.ai';
const DEFAULT_DOH = 'https://cloudflare-dns.com/dns-query';

export async function loadConfig(): Promise<RuntimeConfig> {
  // Only the dev server honors VITE_* values, so a build never bakes an address in.
  if (import.meta.env.DEV && import.meta.env.VITE_API_BASE) {
    return {
      apiBase: validBase(import.meta.env.VITE_API_BASE, 'VITE_API_BASE'),
      svcBase: SVC_BASE,
      grafanaUrl: optionalUrl(import.meta.env.VITE_GRAFANA_URL ?? 'http://localhost:3300'),
      prometheusUrl: optionalUrl(import.meta.env.VITE_PROMETHEUS_URL ?? 'http://localhost:9090'),
      tempoUrl: optionalUrl(import.meta.env.VITE_TEMPO_URL),
      publicFrontHost: DEFAULT_FRONT,
      dohUrl: optionalUrl(import.meta.env.VITE_DOH_URL ?? DEFAULT_DOH),
    };
  }
  let res: Response;
  try {
    res = await fetch(`${import.meta.env.BASE_URL}config.json`, { cache: 'no-store' });
  } catch (e) {
    throw new ConfigError(`config.json을 읽지 못했습니다: ${e instanceof Error ? e.message : String(e)}`);
  }
  if (!res.ok) throw new ConfigError(`config.json 응답이 ${res.status}입니다.`);
  let body: unknown;
  try {
    body = await res.json();
  } catch {
    throw new ConfigError('config.json이 JSON이 아닙니다.');
  }
  const o = (typeof body === 'object' && body !== null ? body : {}) as Record<string, unknown>;
  const host = typeof o.publicFrontHost === 'string' && /^[a-zA-Z0-9.-]+$/.test(o.publicFrontHost) ? o.publicFrontHost : DEFAULT_FRONT;
  return {
    apiBase: validBase(o.apiBase, 'config.json'),
    svcBase: SVC_BASE,
    grafanaUrl: optionalUrl(o.grafanaUrl),
    prometheusUrl: optionalUrl(o.prometheusUrl),
    tempoUrl: optionalUrl(o.tempoUrl),
    publicFrontHost: host,
    dohUrl: optionalUrl(o.dohUrl),
  };
}
