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
