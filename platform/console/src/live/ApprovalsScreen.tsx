/**
 * 승인: the three approval kinds of spec r2 (CLOUD-E2E-CONTRACT §8): change (S2), cutover (S3), failback.
 * Approvals stay console-only and the approver must differ from the requester (the API enforces it).
 * Granted approvals come from the plan SSE stream; the change approval's diff / image digest /
 * policy diff wait for the platform-api contract and show "연결 안 됨" until then.
 */
import type { StreamStatus } from '../api/client';
import type { ConsoleView } from '../api/derive';
import type { DevToken, PlanEvent } from '../api/types';
import { ApprovalPanel } from '../components/ApprovalPanel';
import { fmtTime, shortId } from '../lib/format';
import { StreamBadge } from './AgentsScreen';
import { Block, DataTable, Screen } from './ScreenFrame';
import { asRecord, NotConnected, str, Tag } from './live';
import { eventsOf } from './planEvents';

const KINDS = [
  { stage: 'change', title: '변경 승인 (change · S2)', what: 'REHEARSE 통과 뒤 diff · 이미지 digest · 정책 diff에 결속 → 목적지 반영 → 게이트 재대조' },
  { stage: 'cutover', title: '컷오버 승인 (cutover · S3)', what: '계획 · 게이트 결과에 결속 → Executor 컷오버' },
  { stage: 'failback', title: '되돌리기 승인 (failback)', what: '컷오버 뒤 원래 환경으로 되돌림' },
] as const;

export function ApprovalsScreen({ planId, view, events, status, detail, user }: { planId: string | null; view: ConsoleView | null; events: PlanEvent[]; status: StreamStatus; detail: string | null; user: DevToken | null }) {
  const granted = eventsOf(events, ['approval.granted']);
  return (
    <Screen
      title="승인 · change · cutover · failback"
      lead="승인은 이 콘솔에서만 합니다. 승인자는 요청자와 달라야 하며(직무 분리) API가 이를 강제합니다. 승인 기록은 계획 이벤트 흐름에서 실시간으로 받습니다."
      aside={planId ? <StreamBadge status={status} detail={detail} /> : undefined}
    >
      {!planId || !view ? (
        <NotConnected title="계획이 열려 있지 않음" endpoint="GET /v1/plans/{plan_id} · /events (SSE)" reason="승인은 계획마다 있습니다. 위의 계획 ID 칸에서 계획을 여세요(API에 계획 목록 엔드포인트가 없음)" />
      ) : (
        <>
          <Block title="승인 종류">
            <DataTable
              rows={KINDS.map((k) => {
                const mine = granted.filter((e) => str(asRecord(e.data).stage) === k.stage);
                return { k, mine, waiting: view.awaiting?.stage === k.stage };
              })}
              rowKey={(r) => r.k.stage}
              empty="—"
              cols={[
                { key: 'k', header: '종류', weight: 2, render: (r) => r.k.title },
                { key: 's', header: '상태', render: (r) => (r.mine.length ? <Tag variant="success" label="승인됨" /> : r.waiting ? <Tag variant="warning" label="승인 대기" /> : <span className="foot">기록 없음</span>) },
                { key: 'n', header: '승인', numeric: true, render: (r) => `${r.mine.length}건` },
                { key: 'b', header: '결속 대상', weight: 4, render: (r) => <span className="foot">{r.k.what}</span> },
              ]}
            />
            <NotConnected
              title="change 결속 내용 연결 안 됨"
              endpoint="platform-api change 승인 경로 (docs/architecture 계약 문서 대기)"
              reason="diff · 이미지 digest · 정책 diff를 내주는 API 경로와 계약 문서가 아직 없습니다."
            />
          </Block>
          <Block title={`승인 기록 (${granted.length}건)`}>
            <DataTable
              rows={granted}
              rowKey={(e) => e.id}
              empty="이 계획에는 승인 기록이 없습니다."
              cols={[
                { key: 't', header: '시각', render: (e) => <span className="mono">{fmtTime(e.at)}</span> },
                { key: 's', header: '종류', render: (e) => str(asRecord(e.data).stage) ?? '—' },
                { key: 'a', header: '승인 주체', weight: 2, render: (e) => <span className="mono">{e.actor.kind} {shortId(e.actor.principal)}</span> },
                { key: 'id', header: '승인 ID', weight: 2, render: (e) => <span className="mono">{str(asRecord(e.data).approval_id) ?? '—'}</span> },
              ]}
            />
          </Block>
        </>
      )}
      {planId && view && <ApprovalPanel planId={planId} view={view} user={user} />}
    </Screen>
  );
}
