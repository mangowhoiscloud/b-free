/** 관측: links to Grafana / Prometheus / Tempo and a few live numbers from Prometheus, as tables. */
import { Link } from '@astryxdesign/core/Link';
import { Block, DataTable, Screen, Val } from './ScreenFrame';
import { Fresh, LiveGate, liveReason, Tag, useConfig, useLive } from './live';
import { fmtNum, parseVector, promPath } from './prom';

const METRICS = [
  { label: '쓰기 (전체)', q: 'sum(rate(chat_writes_total[1m]))', unit: '/s', d: 2, empty: '최근 1분 쓰기 없음' },
  { label: '쓰기 지연 p95', q: 'histogram_quantile(0.95, sum by (le) (rate(chat_write_duration_milliseconds_bucket[5m])))', unit: 'ms', d: 0, empty: '최근 5분 쓰기 없음' },
  { label: 'WebSocket 연결', q: 'sum(chat_ws_connections)', unit: '개', d: 0, empty: '시계열 없음' },
  { label: 'HTTP 요청', q: 'sum(rate(http_server_request_duration_seconds_count[1m]))', unit: '/s', d: 2, empty: '시계열 없음' },
] as const;

type Metric = (typeof METRICS)[number];
const parse = (v: unknown) => parseVector(v)[0]?.value ?? null;

export function ObservabilityScreen() {
  const m0 = useLive('prometheus', promPath(METRICS[0].q), 5000, parse);
  const m1 = useLive('prometheus', promPath(METRICS[1].q), 5000, parse);
  const m2 = useLive('prometheus', promPath(METRICS[2].q), 5000, parse);
  const m3 = useLive('prometheus', promPath(METRICS[3].q), 5000, parse);
  const metricRows: { m: Metric; live: typeof m0 }[] = [
    { m: METRICS[0], live: m0 },
    { m: METRICS[1], live: m1 },
    { m: METRICS[2], live: m2 },
    { m: METRICS[3], live: m3 },
  ];
  const { grafanaUrl, prometheusUrl, tempoUrl } = useConfig();
  const targets = useLive('prometheus', promPath('up'), 5000, parseVector);
  const tempoExplore = grafanaUrl ? `${grafanaUrl}/explore?schemaVersion=1&panes=%7B%22t%22%3A%7B%22datasource%22%3A%22tempo%22%7D%7D` : null;
  const links = [
    { label: 'Grafana', href: grafanaUrl, note: '대시보드 · 로그(Loki) · 트레이스' },
    { label: 'Prometheus', href: prometheusUrl, note: '지표 질의 · 수집 대상' },
    { label: 'Tempo', href: tempoUrl ?? tempoExplore, note: tempoUrl ? '트레이스' : 'Tempo 주소가 없으면 Grafana Explore로 엶(Grafana 주소 필요)' },
  ];
  return (
    <Screen title="관측" lead="OTel Collector → Prometheus(지표) · Tempo(트레이스) · Loki(로그) → Grafana. 숫자는 Prometheus 즉시 질의로 5초마다 읽습니다.">
      <Block title="도구">
        <DataTable
          rows={links}
          rowKey={(l) => l.label}
          empty="—"
          cols={[
            { key: 'l', header: '도구', render: (l) => (l.href ? <Link href={l.href} isExternalLink newTabLabel="새 탭에서 열림">{l.label}</Link> : l.label) },
            { key: 'u', header: '주소', weight: 2, render: (l) => (l.href ? <span className="mono">{l.href.split('?')[0]}</span> : <Val v={null} why="주소 설정 없음(BRAVO_*_URL)" />) },
            { key: 'n', header: '용도', weight: 2, render: (l) => <span className="foot">{l.note}</span> },
          ]}
        />
      </Block>
      <Block title="핵심 지표">
        <DataTable
          rows={metricRows}
          rowKey={(r) => r.m.label}
          empty="—"
          cols={[
            { key: 'l', header: '지표', render: (r) => r.m.label },
            {
              key: 'v',
              header: '값',
              numeric: true,
              render: (r) =>
                r.live.error ? <Val v={null} why={liveReason(r.live.error)} /> : <Val v={r.live.data == null ? null : fmtNum(r.live.data, r.m.d)} unit={r.m.unit} why={r.live.data === undefined ? '불러오는 중…' : r.m.empty} />,
            },
            { key: 'q', header: 'PromQL', weight: 3, render: (r) => <span className="mono foot">{r.m.q}</span> },
          ]}
        />
      </Block>
      <Block title="수집 대상 (up)" meta={<Fresh at={targets.updatedAt} />}>
        <LiveGate live={targets}>
          {(rows) => (
            <DataTable
              rows={rows}
              rowKey={(r) => `${r.labels.job}-${r.labels.instance}`}
              empty="Prometheus에 수집 대상이 없습니다."
              cols={[
                { key: 'j', header: 'job', weight: 2, render: (r) => <span className="mono">{r.labels.job ?? '—'}</span> },
                { key: 'i', header: 'instance', weight: 2, render: (r) => <span className="mono">{r.labels.instance ?? '—'}</span> },
                { key: 'u', header: '상태', render: (r) => <Tag variant={r.value === 1 ? 'success' : 'error'} label={r.value === 1 ? '정상' : '응답 없음'} /> },
              ]}
            />
          )}
        </LiveGate>
        <p className="foot">앱 · Executor 지표는 OTLP로 들어와 job이 따로 보이지 않습니다. 직접 긁는 대상만 이 표에 있습니다.</p>
      </Block>
    </Screen>
  );
}
