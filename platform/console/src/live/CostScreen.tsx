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
import { Block, DataTable, Screen } from './ScreenFrame';
import { NotConnected, useConfig } from './live';

type Phase = 'before' | 'during' | 'after';
/** basis_code (15-r2-contracts §8.3): billed 청구 · estimated 추정 · flat 정액(구독) · api_equivalent API 가격 환산(청구 아님). */
type Basis = 'billed' | 'estimated' | 'flat' | 'api_equivalent' | null;
interface Line {
  id: string;
  resource: string;
  component: string | null;
  environment: string | null;
  provider: string | null;
  unitPrice: number | null;
  unit: string | null;
  priceSource: string | null;
  quantity: number | null;
  quantityUnit: string | null;
  subtotal: number | null;
  basis: Basis;
  basisLabel: string | null;
  inTotal: boolean;
  status: string | null;
  note: string | null;
}
interface PhaseSum {
  label: string | null;
  hours: number | null;
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
  plan: { id: string | null; state: string | null; workload: string | null } | null;
  summary: Record<Phase, PhaseSum> | null;
  extra: { value: number | null; explain: string | null; lowerBound: boolean } | null;
  total: number | null;
  billed: number | null;
  estimated: number | null;
  flat: number | null;
  apiEquivalent: number | null;
  unpriced: number | null;
  lines: Line[];
  daily: StackDay[];
  tag: { status: string | null; label: string | null; reason: string | null } | null;
  actuals: { total: number | null; scope: string | null; estimated: boolean | null; period: string | null } | null;
  notConnected: { provider: string | null; what: string | null; reason: string | null }[];
  notes: string[];
  sources: string[];
}

const n = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v)) ? Number(v) : null);
const PHASE_TEXT: Record<Phase, string> = { before: '이전 전', during: '이전 중', after: '이전 후' };
const BASIS_CODES: Record<string, Basis> = { billed: 'billed', estimated: 'estimated', flat: 'flat', api_equivalent: 'api_equivalent', 청구: 'billed', 추정: 'estimated', '정액(구독)': 'flat', 'API 가격 환산(청구 아님)': 'api_equivalent' };
const COMPONENT_TEXT: Record<string, string> = { source: '출발지', platform: '플랫폼', target: '목적지', edge: '앞단(edge)', llm: 'LLM' };
const strList = (v: unknown) => (Array.isArray(v) ? v : []).map((x) => asStr(x) ?? asStr(asObj(x)?.name) ?? asStr(asObj(x)?.provider)).filter((x): x is string => !!x);

function parseReceipt(body: unknown): Receipt {
  const o = asObj(body) ?? {};
  const period = asObj(o.period) ?? {};
  const totals = asObj(o.totals) ?? {};
  const summaryObj = asObj(o.summary);
  const phaseSum = (ph: Phase): PhaseSum => {
    const x = asObj(summaryObj?.[ph]) ?? {};
    return { label: asStr(x.label), hours: n(x.hours), daily: n(x.daily), total: n(x.total) };
  };
  const me = asObj(o.migration_extra) ?? asObj(totals.migration_extra);
  const meNum = n(o.migration_extra) ?? n(totals.migration_extra);
  const lines: Line[] = (Array.isArray(o.lines) ? o.lines : []).map((x, i) => {
    const l = asObj(x) ?? {};
    const ps = asObj(l.price_source);
    const code = BASIS_CODES[asStr(l.basis_code) ?? ''] ?? BASIS_CODES[asStr(l.basis) ?? ''] ?? null;
    return {
      id: asStr(l.line_id) ?? String(i),
      resource: asStr(l.resource) ?? asStr(l.resource_id) ?? '—',
      component: asStr(l.component),
      environment: asStr(l.environment),
      provider: asStr(l.provider),
      unitPrice: n(l.unit_price),
      unit: asStr(l.unit),
      priceSource: ps ? [asStr(ps.name), asStr(ps.ref), asStr(ps.effective_date)].filter(Boolean).join(' · ') || null : asStr(l.price_source),
      quantity: n(l.quantity),
      quantityUnit: asStr(l.quantity_unit),
      subtotal: n(l.subtotal),
      basis: code,
      basisLabel: asStr(l.basis),
      inTotal: l.in_total === undefined ? code !== 'api_equivalent' : l.in_total === true,
      status: asStr(l.status),
      note: asStr(l.reason) ?? asStr(l.note),
    };
  });
  const daily: StackDay[] = (Array.isArray(o.daily) ? o.daily : [])
    .map((d) => {
      const r = asObj(d) ?? {};
      const day = asStr(r.date);
      const by = asObj(r.by_component) ?? {};
      return day ? { day, parts: Object.fromEntries(Object.entries(by).map(([k, v]) => [COMPONENT_TEXT[k] ?? k, n(v) ?? 0])) } : null;
    })
    .filter((d): d is StackDay => d !== null);
  const tag = asObj(o.tag);
  const act = asObj(o.actuals);
  const bySvc = asObj(act?.by_service);
  const actPeriod = asObj(act?.period);
  return {
    currency: asStr(o.currency) ?? 'USD',
    periodStart: asStr(period.start),
    periodEnd: asStr(period.end),
    days: n(period.days),
    billReceivedAt: asStr(o.bill_received_at),
    priceListAt: asStr(o.price_list_at),
    generatedAt: asStr(o.generated_at),
    plan: asObj(o.plan) ? { id: asStr(asObj(o.plan)?.plan_id), state: asStr(asObj(o.plan)?.state), workload: asStr(asObj(o.plan)?.workload) } : null,
    summary: summaryObj ? { before: phaseSum('before'), during: phaseSum('during'), after: phaseSum('after') } : null,
    extra: me ? { value: n(me.value), explain: asStr(me.explain) ?? asStr(me.formula), lowerBound: me.lower_bound === true } : meNum !== null ? { value: meNum, explain: null, lowerBound: false } : null,
    total: n(totals.total),
    billed: n(totals.billed),
    estimated: n(totals.estimated),
    flat: n(totals.flat),
    apiEquivalent: n(totals.api_equivalent_not_billed),
    unpriced: n(totals.unpriced_lines),
    lines,
    daily,
    tag: tag ? { status: asStr(tag.status), label: asStr(tag.label), reason: asStr(tag.reason) } : null,
    actuals: bySvc ? { total: n(bySvc.total), scope: asStr(bySvc.scope), estimated: typeof bySvc.estimated === 'boolean' ? bySvc.estimated : null, period: actPeriod ? `${asStr(actPeriod.start) ?? ''} – ${asStr(actPeriod.end) ?? asStr(actPeriod.end_exclusive) ?? ''}` : asStr(act?.period) } : null,
    notConnected: (Array.isArray(o.not_connected) ? o.not_connected : []).map((x) => ({ provider: asStr(asObj(x)?.provider), what: asStr(asObj(x)?.what), reason: asStr(asObj(x)?.reason) })),
    notes: strList(o.notes),
    sources: (Array.isArray(o.sources) ? o.sources : []).map((x) => {
      const r = asObj(x);
      return asStr(x) ?? (r ? [asStr(r.name) ?? asStr(r.provider), asStr(r.status)].filter(Boolean).join(' ') : null);
    }).filter((x): x is string => !!x),
  };
}

function csv(r: Receipt): string {
  const head = ['line_id', 'resource', 'component', 'environment', 'unit_price', 'unit', 'quantity', 'quantity_unit', 'subtotal', 'basis', 'in_total', 'price_source'];
  const esc = (v: unknown) => {
    const s = v === null || v === undefined ? '' : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return [head.join(','), ...r.lines.map((l) => [l.id, l.resource, l.component, l.environment, l.unitPrice, l.unit, l.quantity, l.quantityUnit, l.subtotal, l.basisLabel, l.inTotal, l.priceSource].map(esc).join(','))].join('\n');
}

const UNIT_KO: Record<string, string> = { hour: '시간', hours: '시간', 'GB-month': 'GB·월', GB: 'GB', 'key-month': '키·월', event: '건', events: '건', tokens: '토큰', month: '월', 'vCPU-hours': 'vCPU·시간', 'GiB-hours': 'GiB·시간', request: '요청', requests: '요청' };
/** API unit is like "USD/hour": the currency is already in the price, so show "/시간". */
const perUnit = (u: string | null) => {
  if (!u) return '';
  const base = u.replace(/^[A-Z]{3}\//, '');
  return ` /${UNIT_KO[base] ?? base}`;
};
const qty = (v: number, u: string | null) => `${v === 0 ? '0' : Math.abs(v) < 1 ? v.toLocaleString('en-US', { maximumSignificantDigits: 3 }) : v.toLocaleString('en-US', { maximumFractionDigits: 2 })}${u ? ` ${UNIT_KO[u] ?? u}` : ''}`;
const BASIS_TEXT: Record<Exclude<Basis, null>, string> = { billed: '청구', estimated: '추정', flat: '정액(구독)', api_equivalent: 'API 가격 환산(청구 아님)' };

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
  const m = (v: number | null, unitPrice = false) => (v === null ? '—' : !unitPrice && v > 0 && v < 0.005 ? `<${fmtMoney(0.01, cur)}` : fmtMoney(v, cur, unitPrice));
  const download = () => {
    if (!r) return;
    const url = URL.createObjectURL(new Blob([csv(r)], { type: 'text/csv;charset=utf-8' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = `bravo-cost-${scope}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };
  const counted = r ? r.lines.filter((l) => l.basis !== 'api_equivalent') : [];
  const apiEq = r ? r.lines.filter((l) => l.basis === 'api_equivalent') : [];
  const groups = [...new Set(counted.map((l) => l.component ?? '기타'))];
  const unpriced = r ? r.unpriced ?? counted.filter((l) => l.subtotal === null).length : 0;
  const billedShare = r && r.total && r.billed !== null ? Math.round((r.billed / r.total) * 100) : null;
  const subtotalCell = (l: Line) => {
    if (l.subtotal === null) return <span className="meta">{l.basis === 'flat' && l.unitPrice === null ? '가격 미기록' : `— ${l.note ?? '금액 모름'}`}</span>;
    return l.inTotal ? m(l.subtotal) : <span className="muted-num">{m(l.subtotal)} (합계 제외)</span>;
  };
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
                {r.days !== null ? ` (${r.days.toLocaleString('en-US', { maximumFractionDigits: 1 })}일)` : ''} · 통화 {cur}
                {r.plan ? ` · 계획 ${shortId(r.plan.id)} ${r.plan.state ?? ''}` : ' · 계획 없음(단계 구분 없음)'}
              </div>
              <div className="meta">
                청구 자료 기준 {r.billReceivedAt ? fmtTs(r.billReceivedAt) : '— 응답에 없음'} · 추정 단가 기준 {r.priceListAt ? fmtTs(r.priceListAt) : '— 응답에 없음'}
                {r.tag?.label ? ` · component 태그 ${r.tag.label}` : ''}
              </div>
            </div>
          </header>

          {r.notConnected.length > 0 && (
            <section aria-label="연결 안 됨">
              <h3 className="receipt__phase">○ 연결 안 됨 · {r.notConnected.length}개 출처 (금액이 빠진 이유)</h3>
              <DataTable
                rows={r.notConnected}
                rowKey={(c, i) => `${c.provider}-${i}`}
                empty="—"
                cols={[
                  { key: 'p', header: '출처', render: (c) => <span className="mono">{c.provider ?? '—'}</span> },
                  { key: 'w', header: '대상', weight: 3, render: (c) => c.what ?? '—' },
                  { key: 'r', header: '이유', weight: 5, render: (c) => <span className="meta">{c.reason ?? '—'}</span> },
                ]}
              />
            </section>
          )}

          <section aria-label="요약">
            {r.summary ? (
              <table className="sumtable">
                <thead>
                  <tr>
                    <th scope="col" />
                    {(['before', 'during', 'after'] as Phase[]).map((ph) => (
                      <th key={ph} scope="col" className="td-end">
                        {r.summary?.[ph].label ?? PHASE_TEXT[ph]}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <th scope="row">하루 비용</th>
                    {(['before', 'during', 'after'] as Phase[]).map((ph) => (
                      <td key={ph} className="td-end num">
                        {m(r.summary?.[ph].daily ?? null)}
                      </td>
                    ))}
                  </tr>
                  <tr>
                    <th scope="row">구간 합계</th>
                    {(['before', 'during', 'after'] as Phase[]).map((ph) => (
                      <td key={ph} className="td-end num">
                        {m(r.summary?.[ph].total ?? null)}
                      </td>
                    ))}
                  </tr>
                </tbody>
              </table>
            ) : (
              <p className="meta">계획을 고르면 이전 전 · 중 · 후로 나눠 보입니다. 지금은 기간 전체 합계만 있습니다.</p>
            )}
            <p className="receipt__extra">
              {r.plan ? (
                <>
                  이전 중 추가 비용 <b className="kpi__value">{m(r.extra?.value ?? null)}</b>
                  {r.extra?.lowerBound && <span className="meta">하한(가격 없는 항목이 있음)</span>}
                  {r.extra?.explain && <span className="meta num">{r.extra.explain}</span>}
                </>
              ) : (
                <>
                  기간 합계 <b className="kpi__value">{m(r.total)}</b>
                </>
              )}
              {unpriced > 0 && <span className="meta">하한(가격 없는 항목 {unpriced}개)</span>}
              {billedShare !== null && <span className="meta">청구 {billedShare}% · 추정 {100 - billedShare}%</span>}
            </p>
          </section>

          {r.daily.some((d) => Object.keys(d.parts).length > 0) ? <StackedBars title={`일별 비용 · component별 (${cur}, 합계에 들어가는 줄만)`} days={r.daily} unit={cur} format={(v) => fmtMoney(v, cur)} /> : <p className="meta">일별 비용: 응답의 daily에 component별 금액이 없습니다({r.daily.length}일, 합계에 들어가는 줄 기준).</p>}

          <section aria-label="품목">
            <table className="plain receipt__lines">
              <thead>
                <tr>
                  <th scope="col">품목</th>
                  <th scope="col">환경</th>
                  <th scope="col" className="td-end">단가</th>
                  <th scope="col" className="td-end">수량 · 시간</th>
                  <th scope="col" className="td-end">소계</th>
                  <th scope="col">근거</th>
                </tr>
              </thead>
              <tbody>
                {groups.map((g) => {
                  const items = counted.filter((l) => (l.component ?? '기타') === g);
                  const sum = items.filter((l) => l.inTotal && l.subtotal !== null).reduce((a, l) => a + (l.subtotal ?? 0), 0);
                  const missing = items.filter((l) => l.inTotal && l.subtotal === null).length;
                  return [
                    <tr key={`g-${g}`} className="receipt-line--group">
                      <td colSpan={4}>
                        <b>{COMPONENT_TEXT[g] ?? g}</b> <span className="meta">{items.length}줄</span>
                      </td>
                      <td className="td-end num">
                        <b>{m(sum)}</b>
                        {missing > 0 && <span className="meta"> 하한({missing}개 금액 없음)</span>}
                      </td>
                      <td />
                    </tr>,
                    ...items.map((l) => (
                      <tr key={l.id} className="receipt-line--sub">
                        <td>
                          <span title={l.priceSource ?? undefined}>└ {l.resource}</span>
                          {l.priceSource && <div className="meta">{l.priceSource.split(' · ')[0]}</div>}
                        </td>
                        <td className="mono">{l.environment ?? '—'}</td>
                        <td className="td-end num nowrap">{l.unitPrice !== null ? `${m(l.unitPrice, true)}${perUnit(l.unit)}` : <span className="meta">{l.basis === 'flat' ? '가격 미기록' : '—'}</span>}</td>
                        <td className="td-end num nowrap">{l.quantity !== null ? qty(l.quantity, l.quantityUnit) : <span className="meta">사용량 자료 없음</span>}</td>
                        <td className="td-end num">{subtotalCell(l)}</td>
                        <td>{l.basis ? BASIS_TEXT[l.basis] : <span className="meta">— {l.basisLabel ?? '근거 없음'}</span>}</td>
                      </tr>
                    )),
                  ];
                })}
                {counted.length === 0 && (
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
                    합계{unpriced > 0 ? ` · 하한(가격 없는 항목 ${unpriced}개)` : ''}
                  </th>
                  <td className="td-end num">
                    <b>{m(r.total)}</b>
                  </td>
                  <td />
                </tr>
                <tr>
                  <td colSpan={6} className="meta">
                    청구 {m(r.billed)} · 추정 {m(r.estimated)} · 정액(구독) {m(r.flat)}. 같은 자원에 태그 청구가 있으면 추정 줄은 합계에서 뺍니다(합계 제외 표시).
                  </td>
                </tr>
              </tfoot>
            </table>
          </section>

          {apiEq.length > 0 && (
            <section aria-label="API 가격 환산">
              <h3 className="receipt__phase">API 가격 환산(청구 아님) · 합계에 넣지 않음 · {m(r.apiEquivalent)}</h3>
              <DataTable
                rows={apiEq}
                rowKey={(l) => l.id}
                empty="—"
                cols={[
                  { key: 'r', header: '실행 · 자원', weight: 3, render: (l) => <span className="mono">{l.resource}</span> },
                  { key: 'q', header: '수량', numeric: true, px: 140, render: (l) => (l.quantity !== null ? qty(l.quantity, l.quantityUnit) : '—') },
                  { key: 's', header: 'API 가격 환산', numeric: true, px: 140, render: (l) => <span className="muted-num">{m(l.subtotal)}</span> },
                  { key: 'p', header: '가격 출처', weight: 2, render: (l) => <span className="meta">{l.priceSource ?? '—'}</span> },
                ]}
              />
              <p className="meta">구독 경로라 청구되지 않습니다. 같은 사용량을 API로 썼다면의 값입니다.</p>
            </section>
          )}

          <section aria-label="참고 값">
            <p className="meta">
              참고 · Cost Explorer 계정 전체 {r.actuals?.total !== null && r.actuals?.total !== undefined ? m(r.actuals.total) : '— 응답에 없음'}
              {r.actuals?.estimated ? ' (추정값, 하루 지연)' : ''}
              {r.actuals?.period ? ` · ${r.actuals.period}` : ''}
              {r.actuals?.scope ? ` · ${r.actuals.scope}` : ' · 합계에 넣지 않는 참고 값'}
            </p>
            {r.notes.map((t, i) => (
              <p key={i} className="meta">
                {t}
              </p>
            ))}
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
