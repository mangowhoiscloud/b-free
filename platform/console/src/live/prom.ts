import { asArray, asRecord, num, str } from './live';

export interface PromSample { labels: Record<string, string>; value: number | null }

/** Parses a Prometheus instant-vector response (/api/v1/query). */
export function parseVector(v: unknown): PromSample[] {
  const o = asRecord(v);
  if (str(o.status) !== 'success') throw new Error(str(o.error) ?? 'Prometheus 응답 status가 success가 아님');
  return asArray(asRecord(o.data).result).map((r) => {
    const x = asRecord(r);
    const pair = asArray(x.value);
    const raw = pair[1];
    const n = typeof raw === 'string' ? Number(raw) : num(raw);
    return {
      labels: Object.fromEntries(Object.entries(asRecord(x.metric)).map(([k, val]) => [k, String(val)])),
      value: n !== null && Number.isFinite(n) ? n : null,
    };
  });
}

export const promPath = (q: string) => `api/v1/query?query=${encodeURIComponent(q)}`;

export function fmtNum(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined) return '—';
  return v.toLocaleString('ko-KR', { maximumFractionDigits: digits, minimumFractionDigits: 0 });
}

/** Environment label (15-r2-contracts §7.3): every panel groups by `bravo_env`
 *  (source-aws, target-onprem, target-gcp, platform). `deployment_environment_name` is app-set and not used. */
export const ENV_BY = 'bravo_env';
export function envOf(labels: Record<string, string>): string {
  return labels.bravo_env || '(bravo_env 없음)';
}

export interface PromRange {
  labels: Record<string, string>;
  points: [number, number][];
}

/** Parses a Prometheus range-vector response (/api/v1/query_range). Times become epoch ms. */
export function parseMatrix(v: unknown): PromRange[] {
  const o = asRecord(v);
  if (str(o.status) !== 'success') throw new Error(str(o.error) ?? 'Prometheus 응답 status가 success가 아님');
  return asArray(asRecord(o.data).result).map((r) => {
    const x = asRecord(r);
    return {
      labels: Object.fromEntries(Object.entries(asRecord(x.metric)).map(([k, val]) => [k, String(val)])),
      points: asArray(x.values)
        .map((pv) => {
          const a = asArray(pv);
          const t = num(a[0]);
          const val = typeof a[1] === 'string' ? Number(a[1]) : num(a[1]);
          return t !== null && val !== null && Number.isFinite(val) ? ([t * 1000, val] as [number, number]) : null;
        })
        .filter((p): p is [number, number] => p !== null),
    };
  });
}

export function rangePath(q: string, minutes: number): string {
  const end = Math.floor(Date.now() / 1000);
  const start = end - minutes * 60;
  const step = Math.max(15, Math.round((minutes * 60) / 120));
  return `api/v1/query_range?query=${encodeURIComponent(q)}&start=${start}&end=${end}&step=${step}`;
}
