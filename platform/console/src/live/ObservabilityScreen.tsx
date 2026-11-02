/** 관측: links to Grafana / Prometheus / Tempo and a few live numbers from Prometheus. */
import { Grid } from '@astryxdesign/core/Grid';
import { Link } from '@astryxdesign/core/Link';
import { VStack } from '@astryxdesign/core/Stack';
import { Text } from '@astryxdesign/core/Text';
import { Block, DataTable, Screen, Stat } from './ScreenFrame';
import { Fresh, LiveGate, Tag, useConfig, useLive } from './live';
import { fmtNum, parseVector, promPath, type PromSample } from './prom';

function scalar(rows: PromSample[]): number | null {
  return rows[0]?.value ?? null;
}

function PromStat({ label, query, unit, digits = 1 }: { label: string; query: string; unit?: string; digits?: number }) {
  const live = useLive('prometheus', promPath(query), 5000, parseVector);
  return (
    <LiveGate live={live}>
      {(rows) => {
        const v = scalar(rows);
        return <Stat label={label} value={v === null ? '값 없음' : fmtNum(v, digits)} unit={v === null ? undefined : unit} note={<span className="mono">{query}</span>} />;
      }}
    </LiveGate>
  );
}

export function ObservabilityScreen() {
  const { grafanaUrl, prometheusUrl, tempoUrl } = useConfig();
  const targets = useLive('prometheus', promPath('up'), 5000, parseVector);
  const tempoExplore = grafanaUrl ? `${grafanaUrl}/explore?schemaVersion=1&panes=%7B%22t%22%3A%7B%22datasource%22%3A%22tempo%22%7D%7D` : null;
  const links: { label: string; href: string | null; note: string }[] = [
    { label: 'Grafana', href: grafanaUrl, note: '대시보드 · 로그(Loki) · 트레이스' },
    { label: 'Prometheus', href: prometheusUrl, note: '지표 질의 · 대상 상태' },
    { label: 'Tempo', href: tempoUrl ?? tempoExplore, note: tempoUrl ? '트레이스' : 'Tempo는 호스트 포트가 없어 Grafana 탐색(Explore)으로 엽니다' },
  ];
  return (
    <Screen title="관측" lead="OTel Collector → Prometheus(지표) · Tempo(트레이스) · Loki(로그) → Grafana. 핵심 숫자는 Prometheus에서 5초마다 읽습니다." aside={<Fresh at={targets.updatedAt} />}>
      <Block title="바로 가기">
        <Grid columns={{ minWidth: 220 }} gap={3}>
          {links.map((l) => (
            <VStack key={l.label} gap={1}>
              {l.href ? (
                <Link href={l.href} isExternalLink newTabLabel="새 탭에서 열림" weight="semibold">
                  {l.label}
                </Link>
              ) : (
                <Text weight="semibold">{l.label} · 연결 안 됨 (주소 설정 없음)</Text>
              )}
              <Text type="supporting" color="secondary">
                {l.note}
              </Text>
              {l.href && <span className="mono muted small">{l.href.split('?')[0]}</span>}
            </VStack>
          ))}
        </Grid>
      </Block>
      <Block title="핵심 숫자">
        <Grid columns={{ minWidth: 200 }} gap={3}>
          <PromStat label="초당 쓰기 (전체)" query="sum(rate(chat_writes_total[1m]))" unit="/s" digits={2} />
          <PromStat label="쓰기 지연 p95" query="histogram_quantile(0.95, sum by (le) (rate(chat_write_duration_milliseconds_bucket[5m])))" unit="ms" />
          <PromStat label="WebSocket 연결" query="sum(chat_ws_connections)" digits={0} />
          <PromStat label="HTTP 요청/초" query="sum(rate(http_server_request_duration_seconds_count[1m]))" unit="/s" digits={2} />
        </Grid>
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
                { key: 'u', header: '상태', render: (r) => <Tag variant={r.value === 1 ? 'success' : 'error'} label={r.value === 1 ? 'up' : 'down'} /> },
              ]}
            />
          )}
        </LiveGate>
      </Block>
    </Screen>
  );
}
