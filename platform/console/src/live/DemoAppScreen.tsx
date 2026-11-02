/**
 * 데모 앱 상태: one table, one row per real environment the demo app runs in.
 * Location and readiness come from the collector (namespace `chat` in each cluster, CLOUD-E2E-CONTRACT §3);
 * the public host per environment comes from config (BRAVO_APP_ENVS). Nothing about location is hard-coded.
 */
import { VStack } from '@astryxdesign/core/Stack';
import type { ConsoleView } from '../api/derive';
import { fmtDateTime, fmtMs, shortId } from '../lib/format';
import { distroOf, parseClusters, type ClustersResp } from './ClustersScreen';
import { EdgeBlock } from './EdgeBlock';
import { Block, DataTable, Facts, Screen, Val } from './ScreenFrame';
import { Fresh, liveReason, NotConnected, Tag, useConfig, useLive } from './live';

const APP_NAMESPACE = 'chat';

interface EnvRow {
  key: string;
  where: string;
  context: string | null;
  host: string | null;
  state: 'ok' | 'warn' | 'down' | 'none';
  note: string;
  workloads: string;
}

const STATE: Record<EnvRow['state'], { v: 'success' | 'warning' | 'error' | 'neutral'; l: string }> = {
  ok: { v: 'success', l: '정상' },
  warn: { v: 'warning', l: '준비 중' },
  down: { v: 'error', l: '응답 없음' },
  none: { v: 'neutral', l: '연결 안 됨' },
};

function rowsFrom(c: ClustersResp, appEnvs: { id: string; host: string }[]): EnvRow[] {
  const hostFor = (id: string, ctx: string | null) => appEnvs.find((e) => e.id === id || (ctx && e.id === ctx))?.host ?? null;
  const rows: EnvRow[] = c.clusters.map((cl) => {
    const label = cl.status === 'ok' ? distroOf(cl) : cl.provider;
    const where = `${label} ${cl.cluster_id} · ns ${APP_NAMESPACE}`;
    const base = { key: cl.cluster_id, where, context: cl.context, host: hostFor(cl.cluster_id, cl.context) };
    if (cl.status !== 'ok') return { ...base, state: 'down', note: cl.error ?? '클러스터에 닿지 않음', workloads: '' };
    const apps = cl.workloads.filter((w) => w.namespace === APP_NAMESPACE);
    if (apps.length === 0) return { ...base, state: 'none', note: `ns ${APP_NAMESPACE}에 워크로드 없음(앱 미배포)`, workloads: '' };
    const ready = apps.every((w) => w.desired > 0 && w.ready >= w.desired);
    return { ...base, state: ready ? 'ok' : 'warn', note: ready ? '모든 워크로드 Ready' : '일부 워크로드 준비 안 됨', workloads: apps.map((w) => `${w.kind}/${w.name} ${w.ready}/${w.desired}`).join(' · ') };
  });
  for (const n of c.not_configured)
    rows.push({ key: n.cluster_id, where: `${n.cluster_id} · ns ${APP_NAMESPACE}`, context: null, host: hostFor(n.cluster_id, null), state: 'none', note: n.reason, workloads: '' });
  return rows;
}

export function DemoAppScreen({ view }: { view: ConsoleView | null }) {
  const { appEnvs } = useConfig();
  const clusters = useLive('collector', 'v1/clusters', 5000, parseClusters);
  const receipt = view?.run?.receipt ?? null;
  const run = view?.run ?? null;
  const at = clusters.updatedAt ? new Date(clusters.updatedAt).toLocaleTimeString('ko-KR', { hour12: false }) : null;

  return (
    <Screen title="데모 앱 상태" lead="고객 데모 앱이 도는 실제 환경마다 한 줄입니다. 위치와 준비 상태는 수집기가 각 클러스터의 ns chat에서 읽고, 공개 이름은 설정(BRAVO_APP_ENVS)에서 옵니다.">
      <Block title="환경별 상태" meta={<Fresh at={clusters.updatedAt} label="수집" />}>
        {clusters.error ? (
          <NotConnected endpoint={clusters.endpoint} reason={`수집기에 닿지 못해 환경을 알 수 없습니다. ${liveReason(clusters.error)}`} />
        ) : (
          <DataTable
            rows={clusters.data ? rowsFrom(clusters.data, appEnvs) : []}
            rowKey={(x) => x.key}
            empty="수집기 응답을 기다리는 중…"
            cols={[
              {
                key: 'w', header: '위치', weight: 3,
                render: (x) => (
                  <VStack gap={0}>
                    <span>{x.where}</span>
                    <span className="foot mono">{[x.context && `context ${x.context}`, x.host].filter(Boolean).join(' · ') || '공개 이름 설정 없음'}</span>
                  </VStack>
                ),
              },
              {
                key: 's', header: '상태', weight: 3,
                render: (x) => (
                  <VStack gap={0}>
                    <span><Tag variant={STATE[x.state].v} label={STATE[x.state].l} /></span>
                    <span className="foot">{x.workloads || x.note}</span>
                  </VStack>
                ),
              },
              { key: 'wps', header: '쓰기/s ¹', numeric: true, render: () => <Val v={null} /> },
              { key: 'err', header: '오류율 ¹', numeric: true, render: () => <Val v={null} /> },
              { key: 'p95', header: 'p95 쓰기 ¹', numeric: true, render: () => <Val v={null} /> },
              { key: 'db', header: 'DB 대상 ²', numeric: true, render: () => <Val v={null} /> },
              { key: 'wb', header: '쓰기 차단 ³', numeric: true, render: () => <Val v={null} /> },
              { key: 't', header: '갱신', numeric: true, render: () => <Val v={at} why="수집 전" /> },
            ]}
          />
        )}
        <ol className="foot notes">
          <li>쓰기/s · 오류율 · p95: 이 콘솔은 실제 환경 앱 지표를 읽는 출처가 없습니다.</li>
          <li>DB 대상: 이 콘솔은 PgBouncer(chat-pgbouncer)의 대상 DB를 읽는 출처가 없습니다.</li>
          <li>쓰기 차단: 이 콘솔은 GET /admin/writes(관리 포트, Bearer 토큰 D-26)를 대신 읽는 출처가 없습니다.</li>
        </ol>
        <p className="foot">
          위치 · 상태 출처: 수집기 <span className="mono">GET /v1/clusters</span>(읽기 전용 get/list).
        </p>
      </Block>

      <Block title="마지막 컷오버 영수증" meta={receipt ? <Tag variant={receipt.verdict === 'MATCH' ? 'success' : 'error'} label={receipt.verdict} /> : undefined}>
        {!view ? (
          <NotConnected title="계획 없음" endpoint="GET /v1/plans/{plan_id}" reason="영수증은 계획에 붙습니다. 홈의 이전 계획 표에서 계획을 고르세요." />
        ) : !receipt ? (
          <p className="foot">이 계획에는 컷오버 영수증이 아직 없습니다{run ? ` (실행 ${shortId(run.run_id)} · ${run.status})` : ''}.</p>
        ) : (
          <VStack gap={3}>
            <Facts
              items={[
                { label: '저장 시각', value: fmtDateTime(receipt.created_at) },
                { label: '쓰기 멈춤', value: <Val v={run?.write_pause_ms != null ? fmtMs(run.write_pause_ms) : null} unit="ms" why="값 없음" /> },
                { label: '쓰기 멈춤 예산', value: <Val v={run?.write_pause_budget_ms != null ? fmtMs(run.write_pause_budget_ms) : null} unit="ms" why="실행 응답에 없음" /> },
                { label: '행 수 src / dst', value: <span className="num">{receipt.rows.src ?? '—'} / {receipt.rows.dst ?? '—'}</span> },
                { label: 'PK 집합 해시', value: receipt.pk_hash.src ? (receipt.pk_hash.src === receipt.pk_hash.dst ? '같음' : '다름') : '—' },
                { label: '내용 해시', value: receipt.content_hash.src ? (receipt.content_hash.src === receipt.content_hash.dst ? '같음' : '다름') : '—' },
              ]}
            />
            <DataTable
              rows={run?.stages ?? []}
              rowKey={(s) => `${s.seq}`}
              empty="단계 기록 없음"
              cols={[
                { key: 'n', header: '단계', weight: 3, render: (s) => <span className="mono">{s.name}</span> },
                { key: 'r', header: '결과', render: (s) => s.result },
                { key: 'ms', header: '소요', numeric: true, render: (s) => <Val v={s.ms} unit="ms" why="값 없음" /> },
              ]}
            />
          </VStack>
        )}
      </Block>

      <EdgeBlock />
    </Screen>
  );
}
