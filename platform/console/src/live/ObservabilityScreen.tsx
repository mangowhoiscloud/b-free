/**
 * 관측 · 이전 전후로 지연과 오류가 나빠졌는가 (DESIGN-REFERENCE §4.2).
 * One time range (URL) for every chart and table; golden-signal table first, then charts on one time axis
 * with the open plan's events as vertical lines and the cutover as a band; platform services; Hubble;
 * drill-down links. Data: Prometheus through the console's read-only /svc/prometheus proxy.
 */
import { useQuery } from '@tanstack/react-query';
import { Link } from '@astryxdesign/core/Link';
import { Selector } from '@astryxdesign/core/Selector';
import { VStack } from '@astryxdesign/core/Stack';
import type { ConsoleView } from '../api/derive';
import type { PlanEvent } from '../api/types';
import { fmtCount, fmtLatency, fmtPct } from '../lib/format';
import { LineChart, Timeline, fmtValue, type Band, type Marker, type Series } from './charts';
import { useHashParam } from './hashQuery';
import { Block, DataTable, Screen, StatusText, Val } from './ScreenFrame';
import { getJson, liveReason, NotConnected, useConfig } from './live';
import { ENV_BY, envOf, parseMatrix, parseVector, promPath, type PromRange } from './prom';
import { TimeRangePicker, useTimeWindow, type TimeWindow } from './TimeRange';

/* PromQL from docs/architecture/15-r2-contracts.md §7.4 (demo app RED without kubelet probe paths). */
const REQ = 'http_server_request_duration_seconds';
const APP = `service_name="bravo-demo", http_route!~"/healthz|/readyz"`;
const Q = {
  rate: `sum by (${ENV_BY}) (rate(${REQ}_count{${APP}}[1m]))`,
  // Contract returns a 0–1 ratio over 5m; the screen shows it as a percent.
  errors: `100 * sum by (${ENV_BY}) (rate(${REQ}_count{${APP}, http_response_status_code=~"5.."}[5m])) / sum by (${ENV_BY}) (rate(${REQ}_count{${APP}}[5m]))`,
  // Contract unit is seconds; the screen shows ms.
  latency: (q: number) => `1000 * histogram_quantile(${q}, sum by (${ENV_BY}, le) (rate(${REQ}_bucket{${APP}}[5m])))`,
  writes: `sum by (${ENV_BY}) (rate(chat_writes_total{kind="message"}[1m]))`,
  connected: `max by (${ENV_BY}) (time() - timestamp(target_info{bravo_env!=""}))`,
  svcStatus: 'max by (http_url) (httpcheck_status{http_status_class="2xx"})',
  svcMs: 'max by (http_url) (httpcheck_duration_milliseconds)',
  svcErr: 'max by (http_url, error_message) (httpcheck_error)',
};
const STALE_S = 120;
const PCT: Record<string, number> = { p50: 0.5, p95: 0.95, p99: 0.99 };

function useRange(q: string, w: TimeWindow) {
  const { svcBase } = useConfig();
  return useQuery({
    queryKey: ['prom-range', q, w.from, w.to],
    queryFn: async () => {
      const start = Math.floor(w.from / 1000);
      const end = Math.floor(w.to / 1000);
      const step = Math.max(15, Math.round((end - start) / 120));
      return parseMatrix(await getJson(`${svcBase}/prometheus/api/v1/query_range?query=${encodeURIComponent(q)}&start=${start}&end=${end}&step=${step}`, 15_000));
    },
    refetchInterval: w.refreshMs,
    retry: 0,
  });
}

/** Instant query evaluated at the end of the window, over the whole window (`[Ns]`). */
function useWindowed(build: (win: string) => string, w: TimeWindow) {
  const { svcBase } = useConfig();
  const win = `${Math.max(60, Math.round((w.to - w.from) / 1000))}s`;
  const q = build(win);
  return useQuery({
    queryKey: ['prom-at', q, w.to],
    queryFn: async () => parseVector(await getJson(`${svcBase}/prometheus/${promPath(q)}&time=${Math.floor(w.to / 1000)}`, 15_000)),
    refetchInterval: w.refreshMs,
    retry: 0,
  });
}

const colorOrder = (envs: string[]) => [...envs].sort();
function toSeries(r: PromRange[] | undefined, order: string[], pick: Set<string> | null): Series[] {
  return (r ?? [])
    .map((x) => ({ id: envOf(x.labels), label: envOf(x.labels), points: x.points, colorIndex: order.indexOf(envOf(x.labels)) }))
    .filter((s) => !pick || pick.has(s.id))
    .sort((a, b) => order.indexOf(a.id) - order.indexOf(b.id));
}

function cutoverOf(view: ConsoleView | null) {
  const stages = view?.run?.stages ?? [];
  const timed = stages.map((s) => ({ s, t: Date.parse(s.started_at ?? '') })).filter((x) => Number.isFinite(x.t) && x.s.ms !== null);
  if (!timed.length) return null;
  const first = Math.min(...timed.map((x) => x.t));
  const last = Math.max(...timed.map((x) => x.t + (x.s.ms ?? 0)));
  return { first, last: Math.max(last, first + 1000), spans: timed.map((x) => ({ id: `${x.s.seq}`, label: x.s.name, start: x.t - first, ms: x.s.ms ?? 0 })) };
}

const MARKER_KIND: Record<string, string> = { 'gate.verdict': '게이트', 'approval.granted': '승인', 'run.done': '실행 끝', 'receipt.saved': '영수증' };
function markersOf(events: PlanEvent[]): Marker[] {
  return events
    .filter((e) => MARKER_KIND[e.kind])
    .map((e) => {
      const d = e.data as Record<string, unknown>;
      const extra = typeof d.verdict === 'string' ? ` ${d.verdict}` : typeof d.stage === 'string' ? ` ${d.stage}` : '';
      return { t: Date.parse(e.at), label: `${MARKER_KIND[e.kind]}${extra}` };
    })
    .filter((m) => Number.isFinite(m.t));
}

/** Platform service health from the platform Collector's http_check receiver (contract §7.4). */
function Services({ w }: { w: TimeWindow }) {
  const st = useWindowed(() => Q.svcStatus, w);
  const ms = useWindowed(() => Q.svcMs, w);
  const err = useWindowed(() => Q.svcErr, w);
  const rows = (st.data ?? []).map((r) => {
    const url = r.labels.http_url ?? '—';
    const errs = (err.data ?? []).filter((e) => e.labels.http_url === url && (e.value ?? 0) > 0).map((e) => e.labels.error_message ?? '');
    return { url, ok: r.value === 1, ms: ms.data?.find((m) => m.labels.http_url === url)?.value ?? null, errs };
  });
  return (
    <Block title="플랫폼 서비스">
      {st.error ? (
        <NotConnected endpoint="GET /svc/prometheus (httpcheck_status)" reason={liveReason(st.error)} lastOk={st.dataUpdatedAt || undefined} />
      ) : (
        <DataTable
          rows={rows}
          rowKey={(r) => r.url}
          empty={st.isLoading ? '불러오는 중…' : 'httpcheck_status 시계열이 없습니다.'}
          cols={[
            {
              key: 'u', header: '상태 경로', weight: 3,
              render: (r) => (
                <VStack gap={0}>
                  <span className="mono">{r.url.replace(/^https?:\/\//, '').replace('.svc', '')}</span>
                  {r.errs.length > 0 && <span className="meta">최근 5분 실패: {r.errs.join(' · ')}</span>}
                </VStack>
              ),
            },
            { key: 's', header: '상태', render: (r) => <StatusText h={r.ok ? 'ok' : 'bad'} text={r.ok ? (r.errs.length ? '정상 · 최근 실패' : '정상') : '응답 없음'} /> },
            { key: 'ms', header: '응답', numeric: true, render: (r) => fmtLatency(r.ms) },
          ]}
        />
      )}
      <p className="meta">출처: 플랫폼 Collector http_check(15초마다 GET) · httpcheck_status · httpcheck_duration_milliseconds · httpcheck_error</p>
    </Block>
  );
}

export function ObservabilityScreen({ view, planId, events }: { view: ConsoleView | null; planId: string | null; events: PlanEvent[] }) {
  const { grafanaUrl, tempoUrl } = useConfig();
  const cut = cutoverOf(view);
  const [w, ctl] = useTimeWindow(cut ? [cut.first, cut.last] : null);
  const [envParam, setEnv] = useHashParam('env', '');
  const [pct, setPct] = useHashParam('pct', 'p95');
  const rate = useRange(Q.rate, w);
  const errors = useRange(Q.errors, w);
  const latency = useRange(Q.latency(PCT[pct] ?? 0.95), w);
  const writes = useRange(Q.writes, w);
  const p50 = useWindowed(() => Q.latency(0.5), w);
  const p95 = useWindowed(() => Q.latency(0.95), w);
  const p99 = useWindowed(() => Q.latency(0.99), w);
  const flows = useWindowed((win) => `sum by (${ENV_BY}, verdict) (increase(hubble_flows_processed_total[${win}]))`, w);
  const drops = useWindowed((win) => `sum by (${ENV_BY}, reason) (increase(hubble_drop_total[${win}]))`, w);
  const conn = useWindowed(() => Q.connected, w);

  const allEnvs = colorOrder([
    ...new Set([...[rate, errors, latency, writes].flatMap((q) => (q.data ?? []).map((r) => envOf(r.labels))), ...(conn.data ?? []).map((r) => envOf(r.labels))]),
  ]);
  const ageOf = (env: string) => conn.data?.find((r) => envOf(r.labels) === env)?.value ?? null;
  const hasSeries = (d: PromRange[] | undefined, env: string) => Boolean(d?.some((r) => envOf(r.labels) === env));
  const pick = envParam ? new Set(envParam.split(',')) : null;
  const shown = allEnvs.filter((e) => !pick || pick.has(e));
  const bands: Band[] = cut ? [{ from: cut.first, to: cut.last, label: '컷오버' }] : [];
  const markers = markersOf(events);
  const promErr = rate.error ?? writes.error;
  const domain: [number, number] = [w.from, w.to];
  const last = (d: PromRange[] | undefined, env: string) => {
    const pts = d?.find((r) => envOf(r.labels) === env)?.points ?? [];
    return pts.length ? pts[pts.length - 1]![1] : null;
  };
  const at = (d: { labels: Record<string, string>; value: number | null }[] | undefined, env: string) => d?.find((r) => envOf(r.labels) === env)?.value ?? null;
  const explore = (ds: string) => (grafanaUrl ? `${grafanaUrl}/explore?schemaVersion=1&panes=${encodeURIComponent(JSON.stringify({ a: { datasource: ds } }))}` : null);

  return (
    <Screen title="관측" lead="이전 전후로 지연과 오류가 나빠졌는가" aside={<TimeRangePicker ctl={ctl} updatedAt={rate.dataUpdatedAt} />}>
      {w.missingCutover && <p className="meta">컷오버 ±30분: 열린 계획에 실행 단계 시각이 없어 최근 1시간을 보입니다.</p>}
      <div className="chips" role="group" aria-label="환경 필터">
        <button type="button" className="chip" aria-pressed={!pick} onClick={() => setEnv(null)}>
          환경 전체 {allEnvs.length}
        </button>
        {allEnvs.map((e) => (
          <button key={e} type="button" className="chip" aria-pressed={!!pick?.has(e)} onClick={() => setEnv(e)}>
            {e}
          </button>
        ))}
      </div>

      {promErr ? (
        <Block title="골든 시그널">
          <NotConnected endpoint="GET /svc/prometheus/api/v1/query_range" reason={`Prometheus에 닿지 못했습니다. ${liveReason(promErr)}`} lastOk={rate.dataUpdatedAt || undefined} />
        </Block>
      ) : (
        <>
          <Block title={`골든 시그널 · 환경별 · ${w.label}`}>
            <DataTable
              rows={shown}
              rowKey={(e) => e}
              empty={rate.isLoading ? '불러오는 중…' : '환경 라벨이 붙은 요청 시계열이 이 범위에 없습니다.'}
              cols={[
                { key: 'e', header: '환경', weight: 2, render: (e) => <span className="mono">{e}</span> },
                {
                  key: 'c', header: '연결', weight: 2,
                  render: (e) => {
                    const age = ageOf(e);
                    if (age === null) return <span className="meta">○ 연결 안 됨 · 환경 에이전트 OTLP 수신 없음</span>;
                    return age > STALE_S ? <span className="meta">○ 연결 안 됨 · 마지막 수신 {Math.round(age)}초 전</span> : <StatusText h="ok" text={`수신 ${Math.round(age)}초 전`} />;
                  },
                },
                { key: 'r', header: '요청/s', numeric: true, render: (e) => (hasSeries(rate.data, e) ? fmtValue(last(rate.data, e), 1) : <Val v={null} why="데모 앱 지표 없음" />) },
                { key: 'x', header: '오류율', numeric: true, render: (e) => { if (!hasSeries(rate.data, e)) return <Val v={null} />; const v = last(errors.data, e); return v === null ? <Val v={null} why="트래픽 없음" /> : fmtPct(v); } },
                { key: 'p50', header: 'p50', numeric: true, render: (e) => fmtLatency(at(p50.data, e)) },
                { key: 'p95', header: 'p95', numeric: true, render: (e) => fmtLatency(at(p95.data, e)) },
                { key: 'p99', header: 'p99', numeric: true, render: (e) => fmtLatency(at(p99.data, e)) },
                { key: 'w', header: '쓰기/s', numeric: true, render: (e) => (hasSeries(writes.data, e) ? fmtValue(last(writes.data, e), 2) : hasSeries(rate.data, e) ? <Val v={null} why="쓰기 0건(Pod 시작 뒤)" /> : <Val v={null} />) },
              ]}
            />
            <p className="meta">데모 앱(bravo-demo) 지표, 프로브 경로 제외. 요청/s · 쓰기/s는 범위 끝의 1분 비율, 오류율과 백분위는 5분 창입니다. 연결은 target_info 마지막 수신(120초 넘으면 연결 안 됨). SLO가 정해지지 않아 상태 판정 열은 두지 않습니다. 환경 라벨 bravo_env(15-r2-contracts §7).</p>
          </Block>

          <Block title="요청 · 오류 · 지연 · 쓰기 (같은 시간축)" meta={<Selector label="지연 백분위" isLabelHidden size="sm" width={90} value={pct} onChange={setPct} options={[{ value: 'p50', label: 'p50' }, { value: 'p95', label: 'p95' }, { value: 'p99', label: 'p99' }]} />}>
            <div className="grid-2">
              <LineChart title="요청" unit="요청/s" digits={1} domain={domain} series={toSeries(rate.data, allEnvs, pick)} bands={bands} markers={markers} empty={rate.isLoading ? '불러오는 중…' : `${REQ} 시계열이 이 범위에 없습니다.`} />
              <LineChart title="5xx 오류율" unit="%" digits={1} domain={domain} series={toSeries(errors.data, allEnvs, pick)} bands={bands} markers={markers} format={(v) => fmtPct(v)} empty={errors.isLoading ? '불러오는 중…' : '이 범위에 요청이 없어 오류율을 계산할 수 없습니다.'} />
              <LineChart title={`${pct} 지연`} unit="ms" digits={0} domain={domain} series={toSeries(latency.data, allEnvs, pick)} bands={bands} markers={markers} format={(v) => fmtLatency(v)} empty={latency.isLoading ? '불러오는 중…' : '이 범위에 지연 히스토그램이 없습니다.'} />
              <LineChart title="쓰기" unit="쓰기/s" digits={1} domain={domain} series={toSeries(writes.data, allEnvs, pick)} bands={bands} markers={markers} empty={writes.isLoading ? '불러오는 중…' : 'chat_writes_total 시계열이 이 범위에 없습니다.'} />
            </div>
            <p className="meta">세로선은 열린 계획의 이벤트(게이트 · 승인 · 실행 끝 · 영수증), 회색 띠는 컷오버 구간입니다{planId ? '' : ' · 계획이 열려 있지 않아 표시할 이벤트가 없음'}.</p>
          </Block>
        </>
      )}

      <Block title="컷오버 단계">
        {!planId || !view ? (
          <NotConnected title="계획 없음" endpoint="GET /v1/plans/{plan_id}" reason="단계와 쓰기 정지는 계획의 실행 기록에 있습니다. 홈의 계획 표에서 계획을 고르세요." />
        ) : !cut ? (
          <p className="meta">아직 기록 없음 · 이 계획에는 단계 시각이 있는 실행 기록이 없습니다.</p>
        ) : (
          <>
            <Timeline title={`실행 ${view.run?.run_id.slice(0, 8) ?? ''}`} spans={cut.spans} />
            <p className="meta">
              쓰기 정지 <b className="num">{fmtLatency(view.run?.write_pause_ms)}</b>
              {view.run?.write_pause_budget_ms != null ? ` · 예산 ${fmtLatency(view.run.write_pause_budget_ms)}` : ''} · 영수증 {view.run?.receipt?.verdict ?? '—'} · 출처 run_stages
            </p>
          </>
        )}
      </Block>

      <div className="grid-2">
        <Services w={w} />
        <Block title={`Hubble 흐름 · ${w.label}`}>
          {flows.error ? (
            <NotConnected endpoint="GET /svc/prometheus (hubble_flows_processed_total)" reason={liveReason(flows.error)} lastOk={flows.dataUpdatedAt || undefined} />
          ) : (
            <>
              <DataTable
                rows={flows.data ?? []}
                rowKey={(r) => `${r.labels.bravo_env}-${r.labels.verdict}`}
                empty={flows.isLoading ? '불러오는 중…' : 'hubble_flows_processed_total 시계열이 없습니다.'}
                cols={[
                  { key: 'e', header: '환경', render: (r) => <span className="mono">{envOf(r.labels)}</span> },
                  { key: 'v', header: '판정', render: (r) => <span className="mono">{r.labels.verdict ?? '—'}</span> },
                  { key: 'n', header: '건수', numeric: true, render: (r) => fmtCount(r.value) },
                ]}
              />
              <DataTable
                rows={drops.data ?? []}
                rowKey={(r) => `${r.labels.bravo_env}-${r.labels.reason}`}
                empty={drops.isLoading ? '불러오는 중…' : 'hubble_drop_total 시계열이 없습니다.'}
                cols={[
                  { key: 'e', header: '환경', render: (r) => <span className="mono">{envOf(r.labels)}</span> },
                  { key: 'v', header: '버린 이유', render: (r) => <span className="mono">{r.labels.reason ?? '—'}</span> },
                  { key: 'n', header: '건수', numeric: true, render: (r) => fmtCount(r.value) },
                ]}
              />
              <p className="meta">
                Hubble 지표가 없는 환경:{' '}
                {allEnvs.filter((e) => !(flows.data ?? []).some((r) => envOf(r.labels) === e)).map((e) => (e === 'target-onprem' ? '온프렘(Hubble 지표 꺼짐)' : e === 'target-gcp' ? 'GKE(Dataplane V2 지표 꺼짐)' : e === 'platform' ? '플랫폼(Hubble 지표 꺼짐)' : e)).join(' · ') || '없음'}
                . 출처 15-r2-contracts §7.6.
              </p>
            </>
          )}
        </Block>
      </div>

      <Block title="드릴다운 · 트레이스 · 로그">
        <NotConnected title="트레이스 · 로그 목록 연결 안 됨" endpoint="Tempo · Loki 질의" reason="이 콘솔은 Tempo와 Loki를 직접 읽는 출처가 없습니다. 아래 도구 링크로 엽니다" />
        <DataTable
          rows={[
            { label: 'Grafana', href: grafanaUrl, note: '대시보드' },
            { label: 'Tempo', href: tempoUrl ?? explore('tempo'), note: '트레이스(Explore)' },
            { label: 'Loki', href: explore('loki'), note: '로그(Explore)' },
          ]}
          rowKey={(l) => l.label}
          empty="—"
          cols={[
            { key: 'l', header: '도구', render: (l) => (l.href ? <Link href={l.href} isExternalLink newTabLabel="새 탭에서 열림">{l.label}</Link> : l.label) },
            { key: 'u', header: '주소', weight: 3, render: (l) => (l.href ? <span className="mono meta">{l.href.split('?')[0]}</span> : <Val v={null} why="주소 설정 없음 · BRAVO_GRAFANA_URL" />) },
            { key: 'n', header: '용도', weight: 2, render: (l) => <span className="meta">{l.note}</span> },
          ]}
        />
      </Block>
    </Screen>
  );
}
