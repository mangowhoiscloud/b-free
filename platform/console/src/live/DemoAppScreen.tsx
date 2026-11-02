/**
 * 데모 앱 상태: app-src / app-dst health and status, write rate (Prometheus), replication and
 * active side (Executor readiness), public front, latest cutover receipt (open plan).
 */
import { Grid } from '@astryxdesign/core/Grid';
import { VStack } from '@astryxdesign/core/Stack';
import { Text } from '@astryxdesign/core/Text';
import type { ConsoleView } from '../api/derive';
import { fmtDateTime, fmtMs, shortId } from '../lib/format';
import { Block, DataTable, Screen, Stat } from './ScreenFrame';
import { asRecord, Fresh, LiveGate, NotConnected, num, str, Tag, useLive } from './live';
import { fmtNum, parseVector, promPath } from './prom';
import { EdgeBlock, WriteSwitchBlock } from './EdgeBlock';

interface AppStatus { envLabel: string | null; instance: string | null; pollAgeMs: number | null; ws: number | null }
const parseStatus = (v: unknown): AppStatus => {
  const o = asRecord(v);
  return { envLabel: str(asRecord(o.env).label), instance: str(o.instance), pollAgeMs: num(o.poll_age_ms), ws: num(o.ws_connections) };
};
const parseHealth = (v: unknown) => str(asRecord(v).status) ?? 'unknown';

function AppSide({ side }: { side: 'app-src' | 'app-dst' }) {
  const health = useLive(side, 'healthz', 3000, parseHealth);
  const status = useLive(side, 'api/status', 3000, parseStatus);
  const ok = health.data === 'ok' && !health.error;
  return (
    <Block title={side === 'app-src' ? '앱 src (원본 환경)' : '앱 dst (목적지 환경)'} meta={<><Tag variant={ok ? 'success' : 'error'} label={ok ? '정상' : '연결 안 됨'} /><Fresh at={health.updatedAt} /></>}>
      <LiveGate live={health}>
        {() => (
          <LiveGate live={status}>
            {(s) => (
              <Grid columns={{ minWidth: 120 }} gap={3}>
                <Stat label="환경" value={s.envLabel ?? '—'} note={<span className="mono">{s.instance ?? ''}</span>} />
                <Stat label="폴링 지연" value={fmtNum(s.pollAgeMs, 0)} unit="ms" />
                <Stat label="WebSocket" value={fmtNum(s.ws, 0)} />
              </Grid>
            )}
          </LiveGate>
        )}
      </LiveGate>
    </Block>
  );
}

interface Readiness { lag: number | null; sub: string | null; direction: string | null; rowsSrc: number | null; rowsDst: number | null; schemaMatch: boolean | null }
const parseReadiness = (v: unknown): Readiness => {
  const o = asRecord(v);
  const sh = asRecord(o.schema_hash);
  const rows = asRecord(o.rows);
  return {
    lag: num(o.lag_bytes), sub: str(o.sub_state), direction: str(o.direction), rowsSrc: num(rows.src), rowsDst: num(rows.dst),
    schemaMatch: str(sh.src) && str(sh.dst) ? sh.src === sh.dst : null,
  };
};

export function DemoAppScreen({ view }: { view: ConsoleView | null }) {
  const rate = useLive('prometheus', promPath('sum by (instance, outcome) (rate(chat_writes_total[1m]))'), 3000, parseVector);
  const ready = useLive('executor', 'v1/readiness', 3000, parseReadiness);
  const receipt = view?.run?.receipt ?? null;
  const run = view?.run ?? null;
  return (
    <Screen title="데모 앱 상태" lead="채팅 데모 앱 두 벌(src · dst)의 건강 상태, 쓰기 속도, 복제 · 활성 쪽, 공개 주소, 마지막 컷오버 영수증을 3초마다 읽습니다.">
      <EdgeBlock />
      <Grid columns={{ minWidth: 320 }} gap={4}>
        <AppSide side="app-src" />
        <AppSide side="app-dst" />
      </Grid>
      <WriteSwitchBlock />
      <Block title="쓰기 속도 (최근 1분)" meta={<Fresh at={rate.updatedAt} />}>
        <LiveGate live={rate}>
          {(rows) => (
            <VStack gap={2}>
              <DataTable
                rows={rows}
                rowKey={(r) => `${r.labels.instance}-${r.labels.outcome}`}
                empty="최근 1분 동안 chat_writes_total 시계열이 없습니다(앱에 쓰기가 없음)."
                cols={[
                  { key: 'i', header: '인스턴스', render: (r) => <span className="mono">{r.labels.instance ?? '—'}</span> },
                  { key: 'o', header: '결과', render: (r) => <Tag variant={r.labels.outcome === 'ok' ? 'success' : 'error'} label={r.labels.outcome ?? '—'} /> },
                  { key: 'v', header: '초당 쓰기', render: (r) => <span className="mono">{fmtNum(r.value, 2)}</span> },
                ]}
              />
              <Text type="supporting" color="secondary" as="p">
                출처: Prometheus <span className="mono">rate(chat_writes_total[1m])</span>. loadgen의 확인 응답(ack) 손실 수는 Prometheus로 내보내지 않으므로(스팬만 Tempo로) 손실은 아래 컷오버 영수증의 행 수 비교로 봅니다.
              </Text>
            </VStack>
          )}
        </LiveGate>
      </Block>
      <Block title="복제 · 활성 쪽 (Executor)" meta={<Fresh at={ready.updatedAt} />}>
        <LiveGate live={ready}>
          {(r) => (
            <VStack gap={2}>
              <Grid columns={{ minWidth: 130 }} gap={3}>
                <Stat label="활성 쪽" value={r.direction === 'forward' ? 'src' : r.direction === 'reverse' ? 'dst' : '—'} note={`추정 · 복제 방향 ${r.direction ?? '—'} 기준`} />
                <Stat label="복제 지연" value={fmtNum(r.lag, 0)} unit="B" />
                <Stat label="구독 상태" value={r.sub ?? '—'} />
                <Stat label="행 수 src / dst" value={`${fmtNum(r.rowsSrc, 0)} / ${fmtNum(r.rowsDst, 0)}`} />
                <Stat label="스키마 해시" value={r.schemaMatch === null ? '—' : r.schemaMatch ? '같음' : '다름'} />
              </Grid>
            </VStack>
          )}
        </LiveGate>
      </Block>
      <Block title="마지막 컷오버 영수증" meta={receipt ? <Tag variant={receipt.verdict === 'MATCH' ? 'success' : 'error'} label={receipt.verdict} /> : undefined}>
        {!view ? (
          <NotConnected title="계획이 열려 있지 않음" endpoint="GET /v1/plans/{plan_id}" reason="영수증은 계획에 붙어 있습니다. 위에서 계획 ID를 열면 보입니다(API에 계획 목록 엔드포인트가 없음)" />
        ) : !receipt ? (
          <Text type="supporting" color="secondary" as="p">
            이 계획에는 아직 컷오버 영수증이 없습니다{run ? ` (실행 ${shortId(run.run_id)} 상태 ${run.status})` : ''}.
          </Text>
        ) : (
          <Grid columns={{ minWidth: 150 }} gap={3}>
            <Stat label="판정" value={receipt.verdict} note={fmtDateTime(receipt.created_at)} />
            <Stat label="쓰기 멈춤" value={fmtMs(run?.write_pause_ms)} unit="ms" note={run?.write_pause_budget_ms != null ? `예산 ${fmtMs(run.write_pause_budget_ms)} ms` : '예산 값 없음'} />
            <Stat label="행 수 src / dst" value={`${receipt.rows.src ?? '—'} / ${receipt.rows.dst ?? '—'}`} note={receipt.rows.src !== null && receipt.rows.src === receipt.rows.dst ? '손실 0' : '비교 필요'} />
            <Stat label="PK 해시" value={receipt.pk_hash.src && receipt.pk_hash.src === receipt.pk_hash.dst ? '같음' : '다름/없음'} />
            <Stat label="내용 해시" value={receipt.content_hash.src && receipt.content_hash.src === receipt.content_hash.dst ? '같음' : '다름/없음'} />
            <Stat label="단계 수" value={run?.stages.length ?? 0} note={run?.stages.map((s) => `${s.name} ${fmtMs(s.ms)}ms`).join(' · ')} />
          </Grid>
        )}
      </Block>
    </Screen>
  );
}
