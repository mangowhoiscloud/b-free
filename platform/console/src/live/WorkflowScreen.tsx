/**
 * CI/CD · 워크플로우: plan state machine, gate runs, rehearsal trials, approvals and
 * cutover/failback receipts from the open plan; gate service catalog; CI and Terraform status.
 */
import { Grid } from '@astryxdesign/core/Grid';
import { Text } from '@astryxdesign/core/Text';
import type { StreamStatus } from '../api/client';
import type { ConsoleView } from '../api/derive';
import type { PlanEvent } from '../api/types';
import { StateProgress } from '../components/StateProgress';
import { fmtTime, shortId } from '../lib/format';
import { StreamBadge } from './AgentsScreen';
import { Block, DataTable, Screen, Stat } from './ScreenFrame';
import { asArray, asRecord, Fresh, LiveGate, NotConnected, num, str, Tag, useLive } from './live';
import { eventsOf, foldTrials } from './planEvents';

const parseCatalog = (v: unknown) => {
  const o = asRecord(v);
  const list = Array.isArray(v) ? v : asArray(o.controls ?? o.catalog ?? o.items);
  return { count: list.length, ids: list.map((x) => str(asRecord(x).control_id) ?? str(asRecord(x).id) ?? '?') };
};

function dataLine(e: PlanEvent): string {
  const d = asRecord(e.data);
  const keys = ['verdict', 'status', 'stage', 'approval_id', 'receipt_id', 'write_pause_ms', 'reason'];
  return keys.filter((k) => d[k] !== undefined && d[k] !== null).map((k) => `${k}=${typeof d[k] === 'string' ? shortId(d[k] as string, 12) : String(d[k])}`).join(' · ') || '—';
}

export function WorkflowScreen({ planId, view, events, status, detail }: { planId: string | null; view: ConsoleView | null; events: PlanEvent[]; status: StreamStatus; detail: string | null }) {
  const catalog = useLive('gate', 'v1/catalog', 5000, parseCatalog);
  const gates = eventsOf(events, ['gate.verdict']);
  const approvals = eventsOf(events, ['approval.granted']);
  const runs = eventsOf(events, ['run.stage', 'run.done', 'receipt.saved']);
  const trials = foldTrials(events);
  return (
    <Screen
      title="CI/CD · 워크플로우"
      lead="계획 상태 기계 진행, 게이트 실행, 리허설 시험, 승인, 컷오버 · 페일백 영수증을 계획 이벤트 흐름으로 봅니다. 승인은 콘솔에서만 하고 승인자는 요청자와 달라야 합니다."
      aside={planId ? <StreamBadge status={status} detail={detail} /> : undefined}
    >
      <Grid columns={{ minWidth: 300 }} gap={4}>
        <Block title="GitHub Actions CI" meta={<Tag variant="neutral" label="연결 안 됨" />}>
          <Text type="supporting" color="secondary" as="p">
            연결 안 됨 (저장소 push 보류) · 저장소가 아직 원격에 올라가지 않아 워크플로 실행 기록이 없습니다. 가짜 실행은 보여 주지 않습니다.
          </Text>
        </Block>
        <Block title="Terraform 환경" meta={<Tag variant="neutral" label="연결 안 됨" />}>
          <Text type="supporting" color="secondary" as="p">
            연결 안 됨 · 이유: <span className="mono">terraform output</span>을 읽기 전용으로 내주는 백엔드 엔드포인트가 아직 없습니다(infra/*는 다른 작업이 진행 중). 클러스터 화면에서 실제 EKS 상태를 수집기로 봅니다.
          </Text>
        </Block>
        <Block title="게이트 서비스" meta={<Fresh at={catalog.updatedAt} />}>
          <LiveGate live={catalog}>
            {(c) => <Stat label="통제 카탈로그" value={c.count} unit="개" note={<span className="mono">{c.ids.slice(0, 6).join(' · ')}</span>} />}
          </LiveGate>
        </Block>
      </Grid>
      {!planId || !view ? (
        <NotConnected title="계획이 열려 있지 않음" endpoint="GET /v1/plans/{plan_id} · /events (SSE)" reason="계획별 진행은 계획을 열어야 보입니다. 위의 계획 ID 칸에서 계획을 여세요(API에 계획 목록 엔드포인트가 없음)" />
      ) : (
        <>
          <StateProgress view={view} />
          <Grid columns={{ minWidth: 160 }} gap={3}>
            <Stat label="게이트 실행" value={gates.length} note={view.gate ? `마지막 ${view.gate.verdict}` : '판정 없음'} />
            <Stat label="리허설 시험" value={trials.length} note={`PASS ${trials.filter((t) => t.verdict === 'PASS').length}`} />
            <Stat label="승인" value={approvals.length} note={view.approval?.approver ? `승인자 ${view.approval.approver}` : '—'} />
            <Stat label="실행 · 영수증" value={eventsOf(events, ['receipt.saved']).length} note={view.run ? `실행 ${shortId(view.run.run_id)} ${view.run.status}` : '실행 없음'} />
          </Grid>
          <Block title="게이트 실행 (gate.verdict)">
            <DataTable
              rows={gates}
              rowKey={(e) => e.id}
              empty="이 계획에는 아직 게이트 판정이 없습니다."
              cols={[
                { key: 't', header: '시각', render: (e) => <span className="mono">{fmtTime(e.at)}</span> },
                { key: 'v', header: '판정', render: (e) => { const v = str(asRecord(e.data).verdict); return <Tag variant={v === 'PASS' ? 'success' : v === 'BLOCK' ? 'error' : 'neutral'} label={v ?? '—'} />; } },
                { key: 'r', header: '사유', weight: 3, render: (e) => <span className="mono">{asArray(asRecord(e.data).reasons).map(String).join(' · ') || '—'}</span> },
                { key: 'm', header: '모드', render: (e) => e.mode },
              ]}
            />
          </Block>
          <Block title="승인 (approval.granted)">
            <DataTable
              rows={approvals}
              rowKey={(e) => e.id}
              empty="이 계획에는 아직 승인이 없습니다."
              cols={[
                { key: 't', header: '시각', render: (e) => <span className="mono">{fmtTime(e.at)}</span> },
                { key: 's', header: '단계', render: (e) => str(asRecord(e.data).stage) ?? '—' },
                { key: 'a', header: '주체', weight: 2, render: (e) => <span className="mono">{e.actor.kind} {shortId(e.actor.principal)}</span> },
                { key: 'id', header: '승인 ID', render: (e) => <span className="mono">{shortId(str(asRecord(e.data).approval_id))}</span> },
              ]}
            />
          </Block>
          <Block title="컷오버 · 페일백 실행과 영수증">
            <DataTable
              rows={runs}
              rowKey={(e) => e.id}
              empty="이 계획에는 아직 실행 이벤트가 없습니다."
              cols={[
                { key: 't', header: '시각', render: (e) => <span className="mono">{fmtTime(e.at)}</span> },
                { key: 'k', header: '종류', render: (e) => <span className="mono">{e.kind}</span> },
                { key: 'n', header: '단계', render: (e) => <span className="mono">{str(asRecord(e.data).name) ?? '—'}{num(asRecord(e.data).ms) !== null ? ` ${num(asRecord(e.data).ms)}ms` : ''}</span> },
                { key: 'd', header: '내용', weight: 3, render: (e) => <span className="mono">{dataLine(e)}</span> },
                { key: 'r', header: '실행', render: (e) => <span className="mono">{shortId(e.run_id)}</span> },
              ]}
            />
          </Block>
        </>
      )}
    </Screen>
  );
}
