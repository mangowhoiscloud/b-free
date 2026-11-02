/**
 * 승인: the three approval kinds of spec r2 (CLOUD-E2E-CONTRACT §8): change (S2), cutover (S3), failback.
 * Approvals stay console-only and the approver must differ from the requester (the API enforces it).
 * Granted approvals come from the plan SSE stream; the change approval's diff / image digest /
 * policy diff wait for the platform-api contract and show "연결 안 됨" until then.
 */
import { Grid } from '@astryxdesign/core/Grid';
import { Text } from '@astryxdesign/core/Text';
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
        <Grid columns={{ minWidth: 300 }} gap={4}>
          {KINDS.map((k) => {
            const mine = granted.filter((e) => str(asRecord(e.data).stage) === k.stage);
            const waiting = view.awaiting?.stage === k.stage;
            return (
              <Block key={k.stage} title={k.title} meta={<Tag variant={mine.length ? 'success' : waiting ? 'warning' : 'neutral'} label={mine.length ? `승인 ${mine.length}` : waiting ? '승인 대기' : '기록 없음'} />}>
                <Text type="supporting" color="secondary" as="p">
                  {k.what}
                </Text>
                <DataTable
                  rows={mine}
                  rowKey={(e) => e.id}
                  empty="이 계획에는 이 종류의 승인 기록이 없습니다."
                  cols={[
                    { key: 't', header: '시각', render: (e) => <span className="mono">{fmtTime(e.at)}</span> },
                    { key: 'a', header: '승인 주체', weight: 2, render: (e) => <span className="mono">{e.actor.kind} {shortId(e.actor.principal)}</span> },
                    { key: 'id', header: '승인 ID', render: (e) => <span className="mono">{shortId(str(asRecord(e.data).approval_id))}</span> },
                  ]}
                />
                {k.stage === 'change' && (
                  <NotConnected
                    endpoint="platform-api change 승인 엔드포인트 (docs/architecture 계약 문서 대기)"
                    reason="diff · 이미지 digest · 정책 diff를 내주는 API 경로와 계약 문서가 아직 없습니다. 생기면 이 자리에서 결속 내용을 보여 줍니다"
                  />
                )}
              </Block>
            );
          })}
        </Grid>
      )}
      {planId && view && <ApprovalPanel planId={planId} view={view} user={user} />}
    </Screen>
  );
}
