/**
 * 관측: per-environment golden signals (rate, errors, p50/p95/p99, write rate) from Prometheus through the
 * console's read-only /svc/prometheus proxy, platform service health, the cutover timeline of the open plan
 * (stages and write pause from its receipt) laid over the charts, Hubble flows and drops, and deep links.
 * Every block says "연결 안 됨" with the endpoint and reason when its source does not answer.
 */
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from '@astryxdesign/core/Link';
import { Selector } from '@astryxdesign/core/Selector';
import type { ConsoleView } from '../api/derive';
import { LineChart, Timeline, fmtValue, type Band, type Series } from './charts';
import { Block, DataTable, Screen, Val } from './ScreenFrame';
import { Fresh, getJson, liveReason, NotConnected, Tag, useConfig } from './live';
import { ENV_BY, envOf, parseMatrix, parseVector, promPath, rangePath, type PromRange } from './prom';

const RANGES = [
  { value: '15', label: '최근 15분' },
  { value: '60', label: '최근 1시간' },
  { value: '360', label: '최근 6시간' },
];

const Q = {
  rate: `sum by (${ENV_BY}) (rate(http_server_request_duration_seconds_count[1m]))`,
  errors: `100 * sum by (${ENV_BY}) (rate(http_server_request_duration_seconds_count{http_response_status_code=~"5.."}[1m])) / sum by (${ENV_BY}) (rate(http_server_request_duration_seconds_count[1m]))`,
  p95: `1000 * histogram_quantile(0.95, sum by (le, ${ENV_BY}) (rate(http_server_request_duration_seconds_bucket[5m])))`,
  writes: `sum by (${ENV_BY}) (rate(chat_writes_total[1m]))`,
};
const QUANTILE = (q: number) => `1000 * histogram_quantile(${q}, sum by (le, ${ENV_BY}) (rate(http_server_request_duration_seconds_bucket[5m])))`;

function usePromRange(q: string, minutes: number) {
  const { svcBase } = useConfig();
  return useQuery({
    queryKey: ['prom-range', q, minutes],
    queryFn: async () => parseMatrix(await getJson(`${svcBase}/prometheus/${rangePath(q, minutes)}`, 15_000)),
    refetchInterval: 15_000,
    retry: 0,
  });
}

function usePromInstant(q: string) {
  const { svcBase } = useConfig();
  return useQuery({ queryKey: ['prom', q], queryFn: async () => parseVector(await getJson(`${svcBase}/prometheus/${promPath(q)}`, 15_000)), refetchInterval: 15_000, retry: 0 });
}

/** Series in a stable order (sorted by environment name) so colors follow the environment, not its rank. */
function toSeries(r: PromRange[] | undefined, order: string[]): Series[] {
  return (r ?? [])
    .map((x) => ({ id: envOf(x.labels), label: envOf(x.labels), points: x.points }))
    .sort((a, b) => order.indexOf(a.id) - order.indexOf(b.id));
}

function Probe() {
  const { svcBase, apiBase } = useConfig();
  const targets = [
    { name: '플랫폼 API', url: `${apiBase}/v1/plans`, shown: 'GET /v1/plans', anyStatus: true },
    { name: '수집기', url: `${svcBase}/collector/healthz`, shown: 'GET /svc/collector/healthz', anyStatus: false },
    { name: '게이트', url: `${svcBase}/gate/healthz`, shown: 'GET /svc/gate/healthz', anyStatus: false },
    { name: 'Prometheus', url: `${svcBase}/prometheus/-/ready`, shown: 'GET /svc/prometheus/-/ready', anyStatus: false },
  ];
  const q = useQuery({
    queryKey: ['platform-health', svcBase, apiBase],
    refetchInterval: 10_000,
    retry: 0,
    queryFn: () =>
      Promise.all(
        targets.map(async (t) => {
          const t0 = performance.now();
          const ctrl = new AbortController();
          const timer = setTimeout(() => ctrl.abort(), 8000);
          try {
            const res = await fetch(t.url, { credentials: 'same-origin', cache: 'no-store', signal: ctrl.signal });
            // The API answering at all (even 401/403) proves it is up; other probes need 2xx.
            const ok = t.anyStatus ? res.status < 500 : res.ok;
            return { ...t, ok, http: String(res.status), ms: Math.round(performance.now() - t0), note: t.anyStatus && !res.ok ? '응답함(권한 응답)' : '' };
          } catch (e) {
            return { ...t, ok: false, http: '—', ms: null, note: ctrl.signal.aborted ? '8초 안에 응답 없음' : e instanceof Error ? e.message : String(e) };
          } finally {
            clearTimeout(timer);
          }
        }),
      ),
  });
  return (
    <Block title="플랫폼 서비스 상태" meta={<Fresh at={q.dataUpdatedAt} />}>
      <DataTable
        rows={q.data ?? []}
        rowKey={(r) => r.name}
        empty="확인하는 중…"
        cols={[
          { key: 'n', header: '서비스', render: (r) => r.name },
          { key: 's', header: '상태', render: (r) => <Tag variant={r.ok ? 'success' : 'error'} label={r.ok ? '정상' : '응답 없음'} /> },
          { key: 'h', header: 'HTTP', numeric: true, render: (r) => r.http },
          { key: 'ms', header: '왕복', numeric: true, render: (r) => <Val v={r.ms} unit="ms" /> },
          { key: 'u', header: '확인 주소 · 비고', weight: 3, render: (r) => <span className="foot"><span className="mono">{r.shown}</span>{r.note ? ` · ${r.note}` : ''}</span> },
        ]}
      />
    </Block>
  );
}

function cutoverOf(view: ConsoleView | null): { band: Band | null; spans: { id: string; label: string; start: number; ms: number }[]; pause: number | null } {
  const stages = view?.run?.stages ?? [];
  const timed = stages.filter((s) => s.started_at && s.ms !== null).map((s) => ({ s, t: Date.parse(s.started_at ?? '') })).filter((x) => Number.isFinite(x.t));
  if (!timed.length) return { band: null, spans: [], pause: view?.run?.write_pause_ms ?? null };
  const first = Math.min(...timed.map((x) => x.t));
  const last = Math.max(...timed.map((x) => x.t + (x.s.ms ?? 0)));
  return {
    band: { from: first, to: Math.max(last, first + 1000), label: `컷오버 ${view?.run?.run_id.slice(0, 8) ?? ''}` },
    spans: timed.map((x) => ({ id: `${x.s.seq}`, label: x.s.name, start: x.t - first, ms: x.s.ms ?? 0 })),
    pause: view?.run?.write_pause_ms ?? null,
  };
}

export function ObservabilityScreen({ view, planId }: { view: ConsoleView | null; planId: string | null }) {
  const { grafanaUrl, tempoUrl } = useConfig();
  const [range, setRange] = useState('60');
  const minutes = Number(range);
  const rate = usePromRange(Q.rate, minutes);
  const errors = usePromRange(Q.errors, minutes);
  const p95 = usePromRange(Q.p95, minutes);
  const writes = usePromRange(Q.writes, minutes);
  const p50n = usePromInstant(QUANTILE(0.5));
  const p95n = usePromInstant(QUANTILE(0.95));
  const p99n = usePromInstant(QUANTILE(0.99));
  const drops = usePromInstant('sum by (reason) (rate(hubble_drop_total[5m]))');
  const flows = usePromInstant('sum by (verdict) (rate(hubble_flows_processed_total[5m]))');

  const envs = [...new Set([rate, errors, p95, writes].flatMap((q) => (q.data ?? []).map((r) => envOf(r.labels))))].sort();
  const cut = cutoverOf(view);
  const bands = cut.band ? [cut.band] : [];
  const promErr = rate.error ?? writes.error;

  const nowRow = (env: string) => {
    const pick = (d: { labels: Record<string, string>; value: number | null }[] | undefined) => d?.find((r) => envOf(r.labels) === env)?.value ?? null;
    const last = (d: PromRange[] | undefined) => {
      const pts = d?.find((r) => envOf(r.labels) === env)?.points ?? [];
      return pts.length ? pts[pts.length - 1]![1] : null;
    };
    return { env, rate: last(rate.data), err: last(errors.data), p50: pick(p50n.data), p95: pick(p95n.data), p99: pick(p99n.data), writes: last(writes.data) };
  };

  const explore = (ds: string) => (grafanaUrl ? `${grafanaUrl}/explore?schemaVersion=1&panes=${encodeURIComponent(JSON.stringify({ a: { datasource: ds } }))}` : null);
  const links = [
    { label: 'Grafana', href: grafanaUrl, note: '대시보드' },
    { label: 'Tempo', href: tempoUrl ?? explore('tempo'), note: '트레이스(Grafana Explore)' },
    { label: 'Loki', href: explore('loki'), note: '로그(Grafana Explore)' },
  ];

  return (
    <Screen
      title="관측"
      lead="환경별 핵심 신호(요청 · 오류 · 지연 · 쓰기), 플랫폼 서비스 상태, 컷오버 구간, 네트워크 흐름을 봅니다. 숫자는 Prometheus를 콘솔의 읽기 전용 프록시로 15초마다 읽습니다."
      aside={<Selector label="기간" isLabelHidden options={RANGES} value={range} onChange={setRange} width={150} size="sm" />}
    >
      <Block title="환경별 핵심 신호" meta={<Fresh at={rate.dataUpdatedAt} />}>
        {promErr ? (
          <NotConnected endpoint="GET /svc/prometheus/api/v1/query_range" reason={`Prometheus에 닿지 못했습니다. ${liveReason(promErr)}`} />
        ) : (
          <>
            <div className="grid-2">
              <LineChart title="요청" unit="요청/s" series={toSeries(rate.data, envs)} bands={bands} empty={rate.isLoading ? '불러오는 중…' : '이 기간에 http_server_request_duration_seconds 시계열이 없습니다.'} />
              <LineChart title="5xx 오류율" unit="%" series={toSeries(errors.data, envs)} bands={bands} empty={errors.isLoading ? '불러오는 중…' : '이 기간에 요청이 없어 오류율을 계산할 수 없습니다.'} />
              <LineChart title="p95 지연" unit="ms" digits={0} series={toSeries(p95.data, envs)} bands={bands} empty={p95.isLoading ? '불러오는 중…' : '이 기간에 지연 히스토그램이 없습니다.'} />
              <LineChart title="쓰기" unit="쓰기/s" series={toSeries(writes.data, envs)} bands={bands} empty={writes.isLoading ? '불러오는 중…' : '이 기간에 chat_writes_total 시계열이 없습니다.'} />
            </div>
            <DataTable
              rows={envs.map(nowRow)}
              rowKey={(r) => r.env}
              empty="환경 라벨이 붙은 시계열이 없습니다."
              cols={[
                { key: 'e', header: '환경 (지금)', weight: 2, render: (r) => <span className="mono">{r.env}</span> },
                { key: 'r', header: '요청', numeric: true, render: (r) => <Val v={r.rate === null ? null : fmtValue(r.rate)} unit="/s" /> },
                { key: 'x', header: '5xx 오류율', numeric: true, render: (r) => <Val v={r.err === null ? null : fmtValue(r.err)} unit="%" /> },
                { key: 'p50', header: 'p50', numeric: true, render: (r) => <Val v={r.p50 === null ? null : fmtValue(r.p50, 0)} unit="ms" /> },
                { key: 'p95', header: 'p95', numeric: true, render: (r) => <Val v={r.p95 === null ? null : fmtValue(r.p95, 0)} unit="ms" /> },
                { key: 'p99', header: 'p99', numeric: true, render: (r) => <Val v={r.p99 === null ? null : fmtValue(r.p99, 0)} unit="ms" /> },
                { key: 'w', header: '쓰기', numeric: true, render: (r) => <Val v={r.writes === null ? null : fmtValue(r.writes)} unit="/s" /> },
              ]}
            />
            <p className="foot">
              환경은 시계열 라벨({ENV_BY} 중 처음 있는 것)로 나눕니다. 회색 띠는 열린 계획의 컷오버 구간입니다{cut.band ? '' : '(열린 계획에 컷오버 기록이 없어 띠가 없음)'}.
            </p>
          </>
        )}
      </Block>

      <Block title="컷오버 타임라인">
        {!planId || !view ? (
          <NotConnected title="계획 없음" endpoint="GET /v1/plans/{plan_id}" reason="컷오버 단계와 쓰기 멈춤은 계획의 실행 기록에 있습니다. 홈의 이전 계획 표에서 계획을 고르세요." />
        ) : cut.spans.length === 0 ? (
          <p className="foot">이 계획에는 단계 시각이 있는 실행 기록이 없습니다.</p>
        ) : (
          <>
            <Timeline title={`실행 ${view.run?.run_id.slice(0, 8) ?? ''} 단계`} spans={cut.spans} />
            <p className="foot">
              쓰기 멈춤 <b className="num">{fmtValue(cut.pause, 0)} ms</b>
              {view.run?.write_pause_budget_ms != null ? ` (예산 ${fmtValue(view.run.write_pause_budget_ms, 0)} ms)` : ''} · 영수증 판정 {view.run?.receipt?.verdict ?? '—'}. 출처: 계획의 실행 기록(run_stages).
            </p>
          </>
        )}
      </Block>

      <Probe />

      <Block title="네트워크 흐름 (Hubble)" meta={<Fresh at={flows.dataUpdatedAt} />}>
        {flows.error ? (
          <NotConnected endpoint="GET /svc/prometheus/api/v1/query (hubble_*)" reason={`Prometheus에 닿지 못했습니다. ${liveReason(flows.error)}`} />
        ) : (
          <div className="grid-2">
            <DataTable
              rows={flows.data ?? []}
              rowKey={(r) => r.labels.verdict ?? '—'}
              empty={flows.isLoading ? '불러오는 중…' : 'hubble_flows_processed_total 시계열이 없습니다(Hubble 지표를 긁지 않음).'}
              cols={[
                { key: 'v', header: '판정', render: (r) => <span className="mono">{r.labels.verdict ?? '—'}</span> },
                { key: 'r', header: '흐름', numeric: true, render: (r) => <Val v={fmtValue(r.value)} unit="/s" /> },
              ]}
            />
            <DataTable
              rows={drops.data ?? []}
              rowKey={(r) => r.labels.reason ?? '—'}
              empty={drops.isLoading ? '불러오는 중…' : 'hubble_drop_total 시계열이 없습니다.'}
              cols={[
                { key: 'v', header: '버린 이유', render: (r) => <span className="mono">{r.labels.reason ?? '—'}</span> },
                { key: 'r', header: '버림', numeric: true, render: (r) => <Val v={fmtValue(r.value)} unit="/s" /> },
              ]}
            />
          </div>
        )}
      </Block>

      <Block title="도구 (Access 뒤)">
        <DataTable
          rows={links}
          rowKey={(l) => l.label}
          empty="—"
          cols={[
            { key: 'l', header: '도구', render: (l) => (l.href ? <Link href={l.href} isExternalLink newTabLabel="새 탭에서 열림">{l.label}</Link> : l.label) },
            { key: 'u', header: '주소', weight: 3, render: (l) => (l.href ? <span className="mono">{l.href.split('?')[0]}</span> : <Val v={null} why="주소 설정 없음(BRAVO_GRAFANA_URL · BRAVO_TEMPO_URL)" />) },
            { key: 'n', header: '용도', weight: 2, render: (l) => <span className="foot">{l.note}</span> },
          ]}
        />
      </Block>
    </Screen>
  );
}
