/**
 * 에이전트 실시간: role-split agents from the open plan's SSE stream
 * (trial.started / trial.proposed / tool.denied / trial.verdict / analysis.done).
 */
import { HStack } from '@astryxdesign/core/Stack';
import { Text } from '@astryxdesign/core/Text';
import type { StreamStatus } from '../api/client';
import type { PlanEvent } from '../api/types';
import { fmtTime, shortId } from '../lib/format';
import { Block, DataTable, Screen } from './ScreenFrame';
import { NotConnected, OkDot, Tag } from './live';
import { AGENT_ROLES, foldTrials, ROLE_LABEL, verifierSummary, type Trial } from './planEvents';

const PHASE_LABEL: Record<Trial['phase'], string> = { requested: '실행 중', proposed: '제안 냄 · 검증 중', verdict: '판정 끝' };

export function StreamBadge({ status, detail }: { status: StreamStatus; detail: string | null }) {
  const ok = status === 'open';
  const label = { connecting: '연결 중', open: 'SSE 실시간', reconnecting: '다시 연결 중', closed: '닫힘', denied: '거부됨' }[status];
  return (
    <HStack gap={2} vAlign="center">
      <OkDot ok={ok ? true : status === 'denied' ? false : null} label={label} />
      <Text type="supporting" color="secondary">
        {label}
        {detail ? ` · ${detail}` : ''}
      </Text>
    </HStack>
  );
}

function verdictTag(t: Trial) {
  if (t.phase !== 'verdict') return <Tag variant="info" label={PHASE_LABEL[t.phase]} />;
  return <Tag variant={t.verdict === 'PASS' ? 'success' : t.verdict === 'FAIL' ? 'error' : 'warning'} label={t.verdict ?? '판정 없음'} />;
}

export function AgentsScreen({ planId, events, status, detail }: { planId: string | null; events: PlanEvent[]; status: StreamStatus; detail: string | null }) {
  if (!planId)
    return (
      <Screen title="에이전트 실시간" lead="역할별 에이전트의 상태 · 도구 호출 판정 · 제안 · Harbor 샌드박스 시험 결과를 계획 이벤트 흐름(SSE)으로 봅니다.">
        <NotConnected title="계획이 열려 있지 않음" endpoint="GET /v1/plans/{plan_id}/events (SSE)" reason="에이전트 이벤트는 계획마다 흐릅니다. 위의 계획 ID 칸에서 계획을 여세요(API에 계획 목록 엔드포인트가 없음)" />
      </Screen>
    );
  const trials = foldTrials(events);
  const denied = trials.flatMap((t) => t.denied.map((d) => ({ ...d, role: t.role, rehearsalId: t.rehearsalId })));
  const analysis = events.filter((e) => e.kind === 'analysis.done');
  return (
    <Screen
      title="에이전트 실시간"
      lead="역할별 에이전트의 상태 · 도구 호출 판정 · 제안 · Harbor 샌드박스 시험 결과를 계획 이벤트 흐름(SSE)으로 봅니다."
      aside={<StreamBadge status={status} detail={detail} />}
    >
      <Block title="역할별 에이전트">
        <DataTable
          rows={AGENT_ROLES.map((role) => {
            const mine = trials.filter((t) => t.role === role);
            return { role, last: mine[0] ?? null, proposals: mine.filter((t) => t.digest).length, denied: mine.reduce((a, t) => a + t.denied.length, 0), n: mine.length };
          })}
          rowKey={(r) => r.role}
          empty="—"
          cols={[
            { key: 'r', header: '역할', weight: 2, render: (r) => ROLE_LABEL[r.role] },
            { key: 's', header: '상태', weight: 2, render: (r) => (r.last ? verdictTag(r.last) : <span className="foot">이 계획에 시험 이벤트 없음</span>) },
            { key: 'task', header: '과제', weight: 2, render: (r) => <span className="mono">{r.last?.task ?? '—'}</span> },
            { key: 'n', header: '시험', numeric: true, render: (r) => `${r.n}회` },
            { key: 'p', header: '제안', numeric: true, render: (r) => `${r.proposals}건` },
            { key: 'd', header: '거부된 호출', numeric: true, render: (r) => `${r.denied}건` },
            { key: 't', header: '마지막', numeric: true, render: (r) => (r.last ? fmtTime(r.last.updatedAt) : '—') },
          ]}
        />
      </Block>
      <Block title={`도구 호출 · 허용 목록 판정 (거부 ${denied.length}건)`}>
        <DataTable
          rows={denied.slice().reverse()}
          rowKey={(d) => d.eventId}
          empty="거부된 도구 호출이 없습니다."
          cols={[
            { key: 't', header: '시각', render: (d) => <span className="mono">{fmtTime(d.at)}</span> },
            { key: 'r', header: '역할', render: (d) => d.role ?? '—' },
            { key: 'tool', header: '도구', weight: 2, render: (d) => <span className="mono">{d.tool ?? '—'}</span> },
            { key: 'dec', header: '판정', render: () => <Tag variant="error" label="거부" /> },
            { key: 'rule', header: '규칙', weight: 2, render: (d) => <span className="mono">{d.rule ?? '—'}</span> },
          ]}
        />
        <p className="foot">
          허용된 호출은 연결 안 됨 · 이유: API는 거부만 <span className="mono">tool.denied</span> 이벤트로 내보냅니다. 허용 기록은 rehearsal 서비스 <span className="mono">GET /v1/trials/{'{id}'}</span>에 있고 서비스 토큰이 필요해 브라우저에서 읽지 않습니다.
        </p>
      </Block>
      <Block title={`Harbor 샌드박스 시험 · 검증기 결과 (${trials.length}건)`}>
        <DataTable
          rows={trials}
          rowKey={(t) => t.rehearsalId}
          empty="이 계획에는 아직 시험(trial.started)이 없습니다."
          cols={[
            { key: 'id', header: '리허설', render: (t) => <span className="mono">{shortId(t.rehearsalId)}</span> },
            { key: 'role', header: '역할', render: (t) => t.role ?? '—' },
            { key: 'a', header: '시도', render: (t) => <span className="mono">{t.attempt ?? '—'}</span> },
            { key: 'p', header: '상태', render: (t) => verdictTag(t) },
            { key: 'dg', header: '제안 digest', render: (t) => <span className="mono">{t.digest ? shortId(t.digest, 12) : '—'}</span> },
            { key: 'v', header: '검증기', weight: 2, render: (t) => <span className="mono">{verifierSummary(t.verifier)}</span> },
            { key: 'why', header: '사유', weight: 2, render: (t) => <span className="mono">{t.reason ?? '—'}</span> },
          ]}
        />
      </Block>
      <Block title="분석 결과 (analysis.done)">
        <DataTable
          rows={analysis}
          rowKey={(e) => e.id}
          empty="이 계획에는 아직 분석 결과 이벤트가 없습니다."
          cols={[
            { key: 't', header: '시각', render: (e) => <span className="mono">{fmtTime(e.at)}</span> },
            { key: 'k', header: '내용 키', weight: 3, render: (e) => <span className="mono">{Object.keys(e.data).slice(0, 8).join(', ') || '—'}</span> },
          ]}
        />
      </Block>
    </Screen>
  );
}
