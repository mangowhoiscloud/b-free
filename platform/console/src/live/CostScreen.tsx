/**
 * 비용 영수증 (#/cost): line items, period totals, before / during / after the migration and the extra cost
 * of the migration, in a printable receipt layout. Data: GET /v1/cost (optionally ?plan_id=), being built by
 * the data thread; its contract will be in docs/architecture/15-r2-contracts.md. Until it answers, the page
 * says "연결 안 됨" with the reason. Parsing is defensive: a missing field shows "—", never a computed guess.
 */
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Button } from '@astryxdesign/core/Button';
import { Selector } from '@astryxdesign/core/Selector';
import { HStack } from '@astryxdesign/core/Stack';
import { errorText } from '../api/client';
import { asObj, asStr } from '../api/normalize';
import { fmtDateTime, shortId } from '../lib/format';
import { useApi, type Session } from '../state';
import { DataTable, Facts, Screen, Val } from './ScreenFrame';
import { NotConnected, Tag, useConfig } from './live';

type Phase = 'before' | 'during' | 'after' | 'unknown';
interface Line {
  resource: string;
  environment: string | null;
  unitPrice: number | null;
  unit: string | null;
  priceSource: string | null;
  quantity: number | null;
  hours: number | null;
  subtotal: number | null;
  basis: 'billed' | 'estimated' | null;
  phase: Phase;
}
interface Receipt {
  currency: string;
  periodStart: string | null;
  periodEnd: string | null;
  generatedAt: string | null;
  lines: Line[];
  totals: { before: number | null; during: number | null; after: number | null; total: number | null; migrationExtra: number | null };
  sources: string[];
}

const n = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v)) ? Number(v) : null);
const PHASES: Record<string, Phase> = { before: 'before', pre: 'before', 이전전: 'before', during: 'during', migration: 'during', 이전중: 'during', after: 'after', post: 'after', 이전후: 'after' };
const PHASE_TEXT: Record<Phase, string> = { before: '이전 전', during: '이전 중', after: '이전 후', unknown: '구간 없음' };

function parseReceipt(body: unknown): Receipt {
  const o = asObj(body) ?? {};
  const period = asObj(o.period) ?? {};
  const totals = asObj(o.totals) ?? {};
  const rawLines = Array.isArray(o.lines) ? o.lines : Array.isArray(o.line_items) ? o.line_items : Array.isArray(o.items) ? o.items : [];
  const lines: Line[] = rawLines.map((x) => {
    const l = asObj(x) ?? {};
    const basis = asStr(l.basis);
    return {
      resource: asStr(l.resource) ?? asStr(l.name) ?? '—',
      environment: asStr(l.environment) ?? asStr(l.env),
      unitPrice: n(l.unit_price),
      unit: asStr(l.unit),
      priceSource: asStr(l.price_source) ?? asStr(l.source),
      quantity: n(l.quantity),
      hours: n(l.hours),
      subtotal: n(l.subtotal) ?? n(l.amount),
      basis: basis === 'billed' || basis === '청구' ? 'billed' : basis === 'estimated' || basis === '추정' ? 'estimated' : null,
      phase: PHASES[asStr(l.phase) ?? ''] ?? 'unknown',
    };
  });
  return {
    currency: asStr(o.currency) ?? 'USD',
    periodStart: asStr(period.start) ?? asStr(o.period_start),
    periodEnd: asStr(period.end) ?? asStr(o.period_end),
    generatedAt: asStr(o.generated_at) ?? asStr(o.as_of),
    lines,
    totals: {
      before: n(totals.before),
      during: n(totals.during),
      after: n(totals.after),
      total: n(totals.total) ?? n(o.total),
      migrationExtra: n(totals.migration_extra) ?? n(o.migration_extra),
    },
    sources: (Array.isArray(o.sources) ? o.sources : []).map((s) => asStr(s) ?? asStr(asObj(s)?.name)).filter((s): s is string => !!s),
  };
}

function money(v: number | null, currency: string) {
  if (v === null) return null;
  try {
    return new Intl.NumberFormat('ko-KR', { style: 'currency', currency, maximumFractionDigits: 2 }).format(v);
  } catch {
    return `${v.toLocaleString('ko-KR', { maximumFractionDigits: 2 })} ${currency}`;
  }
}

export function CostScreen({ session }: { session: Session }) {
  const api = useApi();
  const { apiBase } = useConfig();
  const token = session.user?.token ?? '';
  const [scope, setScope] = useState('all');
  const plans = useQuery({ queryKey: ['plans', token], queryFn: () => api.listPlans(token), retry: 0, staleTime: 30_000 });
  const path = scope === 'all' ? '/v1/cost' : `/v1/cost?plan_id=${encodeURIComponent(scope)}`;
  const cost = useQuery({
    queryKey: ['cost', scope],
    retry: 0,
    refetchInterval: 60_000,
    queryFn: async () => {
      const res = await fetch(`${apiBase}${path}`, { credentials: 'same-origin', headers: { Accept: 'application/json', ...(token ? { 'Cf-Access-Jwt-Assertion': token } : {}) } });
      if (!res.ok) throw new Error(`GET ${path} → ${res.status}${res.status === 404 ? ' (API가 이 경로를 모릅니다)' : ''}`);
      return parseReceipt(await res.json());
    },
  });
  const options = [{ value: 'all', label: '전체(모든 계획)' }, ...(plans.data ?? []).map((p) => ({ value: p.plan_id, label: `${shortId(p.plan_id)} · ${p.workload ?? '—'} · ${p.source ?? '—'} → ${p.target ?? '—'}` }))];
  const r = cost.data;
  const cur = r?.currency ?? 'USD';
  const byPhase = (ph: Phase) => r?.lines.filter((l) => l.phase === ph) ?? [];

  return (
    <Screen
      title="비용 영수증"
      lead="자원별 단가와 사용량으로 계산한 비용입니다. 청구서에서 온 값은 '청구', 단가로 계산한 값은 '추정'으로 표시합니다. 인쇄하면 영수증 모양으로 나옵니다."
      aside={
        <HStack gap={2} vAlign="end">
          <Selector label="범위" isLabelHidden options={options} value={scope} onChange={setScope} width={320} size="sm" />
          <Button label="인쇄" variant="secondary" size="sm" clickAction={() => window.print()} isDisabled={!r} />
        </HStack>
      }
    >
      {cost.error ? (
        <NotConnected endpoint={`GET ${path}`} reason={`비용 데이터를 받지 못했습니다. ${cost.error instanceof Error ? cost.error.message : errorText(cost.error)}. 형식 계약은 docs/architecture/15-r2-contracts.md에 들어갈 예정입니다.`} />
      ) : !r ? (
        <p className="foot">비용을 불러오는 중…</p>
      ) : (
        <article className="receipt" aria-label="비용 영수증">
          <header className="receipt__head">
            <div>
              <div className="receipt__brand">BRAVO · 비용 영수증</div>
              <div className="foot">{scope === 'all' ? '전체 계획' : `계획 ${scope}`}</div>
            </div>
            <Facts
              columns={2}
              items={[
                { label: '기간', value: <span className="num">{fmtDateTime(r.periodStart)} – {fmtDateTime(r.periodEnd)}</span> },
                { label: '발행', value: <span className="num">{fmtDateTime(r.generatedAt)}</span> },
              ]}
            />
          </header>

          {(['before', 'during', 'after', 'unknown'] as Phase[]).map((ph) =>
            byPhase(ph).length === 0 ? null : (
              <section key={ph} className="receipt__section" aria-label={PHASE_TEXT[ph]}>
                <h3 className="receipt__phase">{PHASE_TEXT[ph]}</h3>
                <DataTable
                  rows={byPhase(ph)}
                  rowKey={(l, i) => `${ph}-${l.resource}-${l.environment}-${i}`}
                  empty="—"
                  cols={[
                    { key: 'r', header: '자원', weight: 3, render: (l) => <span className="mono">{l.resource}</span> },
                    { key: 'e', header: '환경', weight: 2, render: (l) => <span className="mono">{l.environment ?? '—'}</span> },
                    { key: 'u', header: '단가', numeric: true, px: 130, render: (l) => <Val v={money(l.unitPrice, cur)} unit={l.unit ? ` /${l.unit}` : undefined} why="없음" /> },
                    { key: 'q', header: '수량 · 시간', numeric: true, px: 120, render: (l) => <Val v={l.quantity !== null || l.hours !== null ? `${l.quantity ?? 1} × ${l.hours ?? '—'}` : null} unit={l.hours !== null ? 'h' : undefined} why="없음" /> },
                    { key: 's', header: '소계', numeric: true, px: 120, render: (l) => <b><Val v={money(l.subtotal, cur)} why="없음" /></b> },
                    { key: 'b', header: '근거', render: (l) => (l.basis ? <Tag variant="neutral" label={l.basis === 'billed' ? '청구' : '추정'} /> : <Val v={null} />) },
                    { key: 'src', header: '단가 출처', weight: 2, render: (l) => <span className="foot">{l.priceSource ?? '—'}</span> },
                  ]}
                />
              </section>
            ),
          )}

          <section className="receipt__totals" aria-label="합계">
            <Facts
              columns="single"
              items={[
                { label: '이전 전', value: <Val v={money(r.totals.before, cur)} why="응답에 없음" /> },
                { label: '이전 중', value: <Val v={money(r.totals.during, cur)} why="응답에 없음" /> },
                { label: '이전 후', value: <Val v={money(r.totals.after, cur)} why="응답에 없음" /> },
                { label: '이전 중 추가 비용', value: <b><Val v={money(r.totals.migrationExtra, cur)} why="응답에 없음" /></b> },
                { label: '합계', value: <b className="receipt__total"><Val v={money(r.totals.total, cur)} why="응답에 없음" /></b> },
              ]}
            />
          </section>

          <footer className="receipt__foot foot">
            데이터 출처: <span className="mono">GET {path}</span>
            {r.sources.length ? ` · ${r.sources.join(' · ')}` : ''} · 화면 시각 {new Date(cost.dataUpdatedAt).toLocaleString('ko-KR', { hour12: false })}
            {r.lines.some((l) => l.basis === 'estimated') ? ' · 추정 줄은 청구서가 아니라 단가 × 사용량입니다.' : ''}
          </footer>
        </article>
      )}
    </Screen>
  );
}
