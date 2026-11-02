/** "이전 중 추가 비용" from GET /v1/cost (cost/v1, 15-r2-contracts §8.2): `migration_extra` is an object. */
export interface Extra {
  extra: number | null;
  lowerBound: boolean;
  hasPlan: boolean;
  currency: string;
  unpriced: number | null;
}

export function extraOf(body: unknown): Extra {
  const o = (typeof body === 'object' && body !== null ? body : {}) as Record<string, unknown>;
  const totals = (o.totals ?? {}) as Record<string, unknown>;
  const raw = o.migration_extra ?? totals.migration_extra;
  const me = typeof raw === 'object' && raw !== null ? (raw as Record<string, unknown>) : null;
  const value = me ? me.value : raw;
  return {
    extra: typeof value === 'number' && Number.isFinite(value) ? value : null,
    lowerBound: me?.lower_bound === true,
    hasPlan: typeof o.plan === 'object' && o.plan !== null,
    currency: typeof o.currency === 'string' ? o.currency : 'USD',
    unpriced: typeof totals.unpriced_lines === 'number' ? totals.unpriced_lines : null,
  };
}

export function extraNote(d: Extra | undefined, err: unknown): string {
  if (err) return `연결 안 됨 · GET /v1/cost · ${err instanceof Error ? err.message : ''}`;
  if (!d) return '불러오는 중…';
  if (!d.hasPlan) return '계획이 없어 이전 단계 구분 없음 · 비용 영수증에서 기간 합계';
  if (d.extra === null) return '응답에 migration_extra 값 없음';
  return d.lowerBound ? `하한(가격 없는 항목${d.unpriced !== null ? ` ${d.unpriced}개` : ''})` : '비용 영수증에서 자세히';
}
