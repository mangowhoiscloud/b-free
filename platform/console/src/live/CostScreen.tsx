/**
 * 비용 영수증 · 이 이전에 얼마가 들었고, 끝나면 얼마인가 (DESIGN-REFERENCE §4.3).
 * Order: header (period, currency, bill-received and price-list times) → summary (before / during / after,
 * daily and period totals, the migration's extra cost with its arithmetic and billed/estimated split) →
 * daily stacked bars → breakdown (resource rows with indented components; source is only 청구 or 추정).
 * Data: GET /v1/cost[?plan_id=] (obs/cost thread; contract pending in docs/architecture/15-r2-contracts.md).
 * Nothing is invented: a field the response lacks shows "—" with the reason; the doc's example numbers never appear.
 */
import { useQuery } from '@tanstack/react-query';
import { Button } from '@astryxdesign/core/Button';
import { Selector } from '@astryxdesign/core/Selector';
import { HStack } from '@astryxdesign/core/Stack';
import { asObj, asStr } from '../api/normalize';
import { fmtMoney, fmtTs, shortId } from '../lib/format';
import { useApi, type Session } from '../state';
import { StackedBars, type StackDay } from './charts';
import { useHashParam } from './hashQuery';
import { Block, Screen } from './ScreenFrame';
import { NotConnected, useConfig } from './live';

type Phase = 'before' | 'during' | 'after';
type Basis = 'billed' | 'estimated' | null;
interface Line {
  resource: string;
  group: string | null;
  environment: string | null;
  unitPrice: number | null;
  unit: string | null;
  quantity: number | null;
  quantityUnit: string | null;
  subtotal: number | null;
  estimate: number | null;
  basis: Basis;
  phase: Phase | null;
  note: string | null;
}
interface PhaseSum {
  daily: number | null;
  total: number | null;
}
interface Receipt {
  currency: string;
  periodStart: string | null;
  periodEnd: string | null;
  days: number | null;
  billReceivedAt: string | null;
  priceListAt: string | null;
  generatedAt: string | null;
  summary: Record<Phase, PhaseSum>;
  extra: number | null;
  total: number | null;
  billed: number | null;
  estimated: number | null;
  lines: Line[];
  daily: StackDay[];
  sources: string[];
}

const n = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v)) ? Number(v) : null);
const PHASE: Record<string, Phase> = { before: 'before', pre: 'before', during: 'during', migration: 'during', after: 'after', post: 'after' };
const PHASE_TEXT: Record<Phase, string> = { before: '이전 전', during: '이전 중', after: '이전 후' };
const basisOf = (v: unknown): Basis => (v === 'billed' || v === '청구' ? 'billed' : v === 'estimated' || v === '추정' ? 'estimated' : null);

function parseReceipt(body: unknown): Receipt {
  const o = asObj(body) ?? {};
  const period = asObj(o.period) ?? {};
  const totals = asObj(o.totals) ?? {};
  const summary = asObj(o.summary) ?? {};
  const phaseSum = (ph: Phase): PhaseSum => {
    const s = asObj(summary[ph]) ?? {};
    return { daily: n(s.daily) ?? n(totals[`${ph}_daily`]), total: n(s.total) ?? n(totals[ph]) };
  };
  const raw = Array.isArray(o.lines) ? o.lines : Array.isArray(o.line_items) ? o.line_items : [];
  const lines: Line[] = raw.map((x) => {
    const l = asObj(x) ?? {};
    return {
      resource: asStr(l.resource) ?? asStr(l.name) ?? '—',
      group: asStr(l.group) ?? asStr(l.parent) ?? asStr(l.component),
      environment: asStr(l.environment) ?? asStr(l.env),
      unitPrice: n(l.unit_price),
      unit: asStr(l.unit),
      quantity: n(l.quantity) ?? n(l.hours),
      quantityUnit: asStr(l.quantity_unit) ?? (n(l.hours) !== null ? '시' : null),
      subtotal: n(l.subtotal) ?? n(l.amount),
      estimate: n(l.estimate),
      basis: basisOf(l.basis ?? l.source),
      phase: PHASE[asStr(l.phase) ?? ''] ?? null,
      note: asStr(l.note) ?? asStr(l.reason),
    };
  });
  const daily: StackDay[] = (Array.isArray(o.daily) ? o.daily : [])
    .map((d) => {
      const r = asObj(d) ?? {};
      const day = asStr(r.date) ?? asStr(r.day);
      const by = asObj(r.by_component) ?? asObj(r.by) ?? {};
      const parts = Object.fromEntries(Object.entries(by).map(([k, v]) => [k, n(v) ?? 0]));
      return day ? { day, parts } : null;
    })
    .filter((d): d is StackDay => d !== null);
  return {
    currency: asStr(o.currency) ?? 'USD',
    periodStart: asStr(period.start) ?? asStr(o.period_start),
    periodEnd: asStr(period.end) ?? asStr(o.period_end),
    days: n(period.days) ?? n(o.days),
    billReceivedAt: asStr(o.bill_received_at) ?? asStr(asObj(o.billing)?.received_at),
    priceListAt: asStr(o.price_list_at) ?? asStr(asObj(o.pricing)?.as_of),
    generatedAt: asStr(o.generated_at) ?? asStr(o.as_of),
    summary: { before: phaseSum('before'), during: phaseSum('during'), after: phaseSum('after') },
    extra: n(totals.migration_extra) ?? n(o.migration_extra),
    total: n(totals.total) ?? n(o.total),
    billed: n(totals.billed),
    estimated: n(totals.estimated),
    lines,
    daily,
    sources: (Array.isArray(o.sources) ? o.sources : []).map((s) => asStr(s) ?? asStr(asObj(s)?.name)).filter((s): s is string => !!s),
  };
}

function csv(r: Receipt): string {
  const head = ['resource', 'group', 'environment', 'phase', 'unit_price', 'unit', 'quantity', 'quantity_unit', 'subtotal', 'basis'];
  const esc = (v: unknown) => {
    const s = v === null || v === undefined ? '' : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return [head.join(','), ...r.lines.map((l) => [l.resource, l.group, l.environment, l.phase, l.unitPrice, l.unit, l.quantity, l.quantityUnit, l.subtotal, l.basis].map(esc).join(','))].join('\n');
}

const BASIS_TEXT = { billed: '청구', estimated: '추정' } as const;

export function CostScreen({ session }: { session: Session }) {
  const api = useApi();
  const { apiBase } = useConfig();
  const token = session.user?.token ?? '';
  const [scope, setScope] = useHashParam('plan', 'all');
  const plans = useQuery({ queryKey: ['plans', token], queryFn: () => api.listPlans(token), retry: 0, staleTime: 30_000 });
  const path = scope === 'all' ? '/v1/cost' : `/v1/cost?plan_id=${encodeURIComponent(scope)}`;
  const cost = useQuery({
    queryKey: ['cost', scope],
    retry: 0,
    refetchInterval: 60_000,
    queryFn: async () => {
      const res = await fetch(`${apiBase}${path}`, { credentials: 'same-origin', headers: { Accept: 'application/json' } });
      if (!res.ok) throw new Error(`HTTP ${res.status}${res.status === 404 ? ' · API가 이 경로를 모릅니다' : ''}`);
      return parseReceipt(await res.json());
    },
  });
  const plan = plans.data?.find((p) => p.plan_id === scope);
  const options = [{ value: 'all', label: '전체 계획' }, ...(plans.data ?? []).map((p) => ({ value: p.plan_id, label: `${shortId(p.plan_id)} · ${p.workload ?? '—'} · ${p.source ?? '—'} → ${p.target ?? '—'}` }))];
  const r = cost.data;
  const cur = r?.currency ?? 'USD';
  const m = (v: number | null, unitPrice = false) => (v === null ? '—' : fmtMoney(v, cur, unitPrice));
  const download = () => {
    if (!r) return;
    const url = URL.createObjectURL(new Blob([csv(r)], { type: 'text/csv;charset=utf-8' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = `bravo-cost-${scope}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };
  const groups = r ? [...new Set(r.lines.map((l) => l.group ?? l.resource))] : [];
  const billedShare = r && r.total && r.billed !== null ? Math.round((r.billed / r.total) * 100) : null;

  return (
    <Screen
      title={`비용 영수증${plan ? ` · ${shortId(plan.plan_id)} (${plan.source ?? '—'} · ${plan.target ?? '—'})` : ''}`}
      lead="이 이전에 얼마가 들었고, 끝나면 얼마인가"
      aside={
        <HStack gap={2} vAlign="center">
          <Selector label="범위" isLabelHidden options={options} value={scope} onChange={(v) => setScope(v === 'all' ? null : v)} width={320} size="sm" />
          <Button label="CSV" variant="secondary" size="sm" clickAction={download} isDisabled={!r || r.lines.length === 0} />
          <Button label="인쇄" variant="secondary" size="sm" clickAction={() => window.print()} isDisabled={!r} />
        </HStack>
      }
    >
      {cost.error ? (
        <Block title="영수증">
          <NotConnected endpoint={`GET ${path}`} reason={`비용 데이터를 받지 못했습니다 · ${cost.error instanceof Error ? cost.error.message : String(cost.error)} · 형식 계약은 docs/architecture/15-r2-contracts.md 예정`} />
        </Block>
      ) : !r ? (
        <p className="meta">비용을 불러오는 중…</p>
      ) : (
        <article className="receipt" aria-label="비용 영수증">
          <header className="receipt__head">
            <div>
              <div className="receipt__brand">BRAVO · 비용 영수증</div>
              <div className="meta">
                기간 {fmtTs(r.periodStart)} – {fmtTs(r.periodEnd)}
                {r.days !== null ? ` (${r.days}일)` : ''} · 통화 {cur}
              </div>
              <div className="meta">
                청구 자료 기준 {r.billReceivedAt ? fmtTs(r.billReceivedAt) : '— 응답에 없음'} · 추정 단가 기준 {r.priceListAt ? fmtTs(r.priceListAt) : '— 응답에 없음'}
              </div>
            </div>
          </header>

          <section aria-label="요약">
            <table className="sumtable">
              <thead>
                <tr>
                  <th scope="col" />
                  {(['before', 'during', 'after'] as Phase[]).map((ph) => (
                    <th key={ph} scope="col" className="td-end">
                      {PHASE_TEXT[ph]}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                <tr>
                  <th scope="row">하루 비용</th>
                  {(['before', 'during', 'after'] as Phase[]).map((ph) => (
                    <td key={ph} className="td-end num">
                      {m(r.summary[ph].daily)}
                    </td>
                  ))}
                </tr>
                <tr>
                  <th scope="row">기간 합계</th>
                  {(['before', 'during', 'after'] as Phase[]).map((ph) => (
                    <td key={ph} className="td-end num">
                      {m(r.summary[ph].total)}
                    </td>
                  ))}
                </tr>
              </tbody>
            </table>
            <p className="receipt__extra">
              이전 중 추가 비용 <b className="kpi__value">{m(r.extra)}</b>
              {r.extra === null && <span className="meta"> 응답에 migration_extra 없음</span>}
              {r.summary.during.total !== null && r.summary.before.total !== null && (
                <span className="meta num">
                  {' '}
                  ({m(r.summary.during.total)} − {m(r.summary.before.total)}, 응답의 구간 합계)
                </span>
              )}
              {billedShare !== null && <span className="meta"> · 청구 {billedShare}% · 추정 {100 - billedShare}%</span>}
            </p>
          </section>

          {r.daily.length > 0 ? <StackedBars title="일별 비용 · component별" days={r.daily} unit={cur} format={(v) => fmtMoney(v, cur)} /> : <p className="meta">일별 비용: 응답에 daily가 없어 막대를 그리지 않습니다.</p>}

          <section aria-label="품목">
            <table className="plain receipt__lines">
              <thead>
                <tr>
                  <th scope="col">품목</th>
                  <th scope="col">환경</th>
                  <th scope="col" className="td-end">단가</th>
                  <th scope="col" className="td-end">수량 · 시간</th>
                  <th scope="col" className="td-end">소계</th>
                  <th scope="col">출처</th>
                </tr>
              </thead>
              <tbody>
                {groups.map((g) => {
                  const items = r.lines.filter((l) => (l.group ?? l.resource) === g);
                  const sum = items.every((l) => l.subtotal !== null) ? items.reduce((a, l) => a + (l.subtotal ?? 0), 0) : null;
                  const grouped = items.some((l) => l.group);
                  return [
                    grouped ? (
                      <tr key={`g-${g}`} className="receipt-line--group">
                        <td colSpan={4}>
                          <b>{g}</b>
                        </td>
                        <td className="td-end num">
                          <b>{m(sum)}</b>
                        </td>
                        <td />
                      </tr>
                    ) : null,
                    ...items.map((l, i) => (
                      <tr key={`${g}-${i}`} className={grouped ? 'receipt-line--sub' : undefined}>
                        <td>
                          {grouped ? '└ ' : ''}
                          {l.resource}
                          {l.basis === 'estimated' && l.subtotal === null ? ' *' : ''}
                        </td>
                        <td className="mono">{l.environment ?? '—'}</td>
                        <td className="td-end num">{l.unitPrice !== null ? `${m(l.unitPrice, true)}${l.unit ? ` /${l.unit}` : ''}` : '—'}</td>
                        <td className="td-end num">{l.quantity !== null ? `${l.quantity.toLocaleString('en-US')}${l.quantityUnit ? ` ${l.quantityUnit}` : ''}` : <span className="meta">사용량 자료 없음</span>}</td>
                        <td className="td-end num">
                          {m(l.subtotal)}
                          {l.basis === 'billed' && l.estimate !== null && <span className="muted-num"> ({m(l.estimate)} 추정)</span>}
                        </td>
                        <td>{l.basis ? BASIS_TEXT[l.basis] : <span className="meta">— {l.note ?? '근거 없음'}</span>}</td>
                      </tr>
                    )),
                  ];
                })}
                {r.lines.length === 0 && (
                  <tr>
                    <td colSpan={6} className="meta">
                      응답에 품목이 없습니다.
                    </td>
                  </tr>
                )}
              </tbody>
              <tfoot>
                <tr>
                  <th scope="row" colSpan={4}>
                    합계
                  </th>
                  <td className="td-end num">
                    <b>{m(r.total)}</b>
                  </td>
                  <td />
                </tr>
                <tr>
                  <td colSpan={6} className="meta">
                    청구(Cost Explorer) {m(r.billed)} · 추정(목록가 × 가동 시간) {m(r.estimated)}. 같은 품목에 둘 다 있으면 청구를 쓰고 추정은 괄호의 회색 숫자로 둡니다. * 사용량 기반 추정 줄.
                  </td>
                </tr>
              </tfoot>
            </table>
          </section>

          <footer className="receipt__foot meta">
            데이터 출처 <span className="mono">GET {path}</span>
            {r.sources.length ? ` · ${r.sources.join(' · ')}` : ''} · 발행 {fmtTs(r.generatedAt)} · 화면 {fmtTs(cost.dataUpdatedAt)}
          </footer>
        </article>
      )}
    </Screen>
  );
}
