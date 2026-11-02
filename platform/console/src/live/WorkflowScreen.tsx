/**
 * CI/CD · 워크플로우: plan state machine, gate runs, rehearsal trials, approvals and
 * cutover/failback receipts from the open plan; gate service catalog; CI and Terraform status.
 */
import type { StreamStatus } from '../api/client';
import type { ConsoleView } from '../api/derive';
import type { PlanEvent } from '../api/types';
import { StateProgress } from '../components/StateProgress';
import { fmtTime, shortId } from '../lib/format';
import { StreamBadge } from './AgentsScreen';
import { Block, DataTable, Facts, Screen, Val } from './ScreenFrame';
import { asArray, asRecord, liveReason, NotConnected, num, str, Tag, useLive } from './live';
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
      <Block title="연결 상태">
        <DataTable
          rows={[
            { k: 'GitHub Actions CI', state: 'none' as const, why: '이 콘솔은 CI 실행 기록을 읽는 출처가 없습니다.' },
            { k: 'Terraform 환경', state: 'none' as const, why: '이 콘솔은 terraform output을 읽는 출처가 없습니다. 클러스터 상태는 클러스터 화면에서 수집기로 봅니다.' },
            {
              k: '게이트 서비스',
              state: catalog.error ? ('down' as const) : catalog.data ? ('ok' as const) : ('none' as const),
              why: catalog.error ? `${liveReason(catalog.error)} · ${catalog.endpoint}` : catalog.data ? `통제 ${catalog.data.count}개: ${catalog.data.ids.join(' · ')}` : '불러오는 중…',
            },
          ]}
          rowKey={(r) => r.k}
          empty="—"
          cols={[
            { key: 'k', header: '대상', render: (r) => r.k },
            { key: 's', header: '상태', render: (r) => <Tag variant={r.state === 'ok' ? 'success' : r.state === 'down' ? 'error' : 'neutral'} label={r.state === 'ok' ? '정상' : r.state === 'down' ? '응답 없음' : '연결 안 됨'} /> },
            { key: 'w', header: '내용', weight: 4, render: (r) => <span className="foot">{r.why}</span> },
          ]}
        />
      </Block>
      {!planId || !view ? (
        <NotConnected title="계획이 열려 있지 않음" endpoint="GET /v1/plans/{plan_id} · /events (SSE)" reason="계획별 진행은 계획을 열어야 보입니다. 위의 계획 ID 칸에서 계획을 여세요(API에 계획 목록 엔드포인트가 없음)" />
      ) : (
        <>
          <StateProgress view={view} />
          <Block title="이 계획의 기록 수">
            <Facts
              items={[
                { label: '게이트 실행', value: <span className="num">{gates.length}회 · 마지막 {view.gate?.verdict ?? '—'}</span> },
                { label: '리허설 시험', value: <span className="num">{trials.length}회 · PASS {trials.filter((t) => t.verdict === 'PASS').length}</span> },
                { label: '승인', value: <span className="num">{approvals.length}건 · {view.approval?.approver ?? '승인자 없음'}</span> },
                { label: '영수증', value: <span className="num">{eventsOf(events, ['receipt.saved']).length}건 · {view.run ? `실행 ${shortId(view.run.run_id)} ${view.run.status}` : '실행 없음'}</span> },
              ]}
            />
          </Block>
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
                { key: 'n', header: '단계', weight: 2, render: (e) => <span className="mono">{str(asRecord(e.data).name) ?? '—'}</span> },
                { key: 'ms', header: '소요', numeric: true, render: (e) => (e.kind === 'run.stage' ? <Val v={num(asRecord(e.data).ms)} unit="ms" why="값 없음" /> : '') },
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
