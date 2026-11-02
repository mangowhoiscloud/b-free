/**
 * 승인: the three approval kinds of spec r2 (CLOUD-E2E-CONTRACT §8): change (S2), cutover (S3), failback.
 * Approvals stay console-only and the approver must differ from the requester (the API enforces it).
 * Granted approvals come from the plan SSE stream; the change approval's diff / image digest /
 * policy diff wait for the platform-api contract and show "연결 안 됨" until then.
 */
import type { StreamStatus } from '../api/client';
import type { ConsoleView } from '../api/derive';
import type { DevToken, PlanEvent } from '../api/types';
import { useQuery } from '@tanstack/react-query';
import { errorText } from '../api/client';
import { ApprovalPanel } from '../components/ApprovalPanel';
import { useApi } from '../state';
import { fmtTime, shortId } from '../lib/format';
import { StreamBadge } from './AgentsScreen';
import { Block, DataTable, Screen, Val } from './ScreenFrame';
import { asRecord, Fresh, NotConnected, str, Tag } from './live';
import { eventsOf } from './planEvents';

const KINDS = [
  { stage: 'change', title: '변경 승인 (change · S2)', what: 'REHEARSE 통과 뒤 diff · 이미지 digest · 정책 diff에 결속 → 목적지 반영 → 게이트 재대조' },
  { stage: 'cutover', title: '컷오버 승인 (cutover · S3)', what: '계획 · 게이트 결과에 결속 → Executor 컷오버' },
  { stage: 'failback', title: '되돌리기 승인 (failback)', what: '컷오버 뒤 원래 환경으로 되돌림' },
] as const;

function short(v: unknown, n = 12) {
  return typeof v === 'string' && v ? <span className="mono">{shortId(v.replace(/^sha256:/, ''), n)}</span> : <Val v={null} />;
}

/** change (S2) approvals' binding from GET /v1/plans/{id}/changes. */
function ChangesBlock({ planId, token }: { planId: string; token: string }) {
  const api = useApi();
  const q = useQuery({ queryKey: ['changes', planId, token], queryFn: () => api.listChanges(token, planId), refetchInterval: 5000, retry: 0 });
  return (
    <Block title={`변경(change) 결속 내용${q.data ? ` (${q.data.length}건)` : ''}`} meta={<Fresh at={q.dataUpdatedAt} />}>
      {q.error ? (
        <NotConnected endpoint={`GET /v1/plans/${shortId(planId)}/changes`} reason={errorText(q.error)} />
      ) : (
        <DataTable
          rows={q.data ?? []}
          rowKey={(c, i) => String(c.change_id ?? i)}
          empty={q.isLoading ? '불러오는 중…' : '이 계획에는 변경 기록이 없습니다.'}
          cols={[
            { key: 'id', header: '변경', render: (c) => short(c.change_id, 8) },
            { key: 's', header: '상태', render: (c) => (typeof c.status === 'string' ? <span className="mono">{c.status}</span> : <Val v={null} />) },
            { key: 'img', header: '이미지 digest', weight: 2, render: (c) => short(c.image_digest) },
            { key: 'diff', header: 'diff digest', weight: 2, render: (c) => short(c.diff_digest) },
            { key: 'pol', header: '정책 diff digest', weight: 2, render: (c) => short(c.policy_diff_digest) },
            { key: 'dep', header: '배포 digest', weight: 2, render: (c) => short(c.deployed_digest) },
            { key: 'r', header: '사유', weight: 2, render: (c) => <span className="foot">{typeof c.reason === 'string' ? c.reason : '—'}</span> },
          ]}
        />
      )}
    </Block>
  );
}

export function ApprovalsScreen({ planId, view, events, status, detail, user }: { planId: string | null; view: ConsoleView | null; events: PlanEvent[]; status: StreamStatus; detail: string | null; user: DevToken | null }) {
  const granted = eventsOf(events, ['approval.granted']);
  return (
    <Screen
      title="승인 기록"
      lead="누가 무엇을 승인했고, 무엇이 승인을 기다리는가 (change · cutover · failback, 요청자 ≠ 승인자)"
      aside={planId ? <StreamBadge status={status} detail={detail} /> : undefined}
    >
      {!planId || !view ? (
        <NotConnected title="계획이 열려 있지 않음" endpoint="GET /v1/plans/{plan_id} · /events (SSE)" reason="승인은 계획마다 있습니다. 홈의 계획 표에서 계획을 고르세요." />
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
          </Block>
          <ChangesBlock planId={planId} token={user?.token ?? ''} />
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
