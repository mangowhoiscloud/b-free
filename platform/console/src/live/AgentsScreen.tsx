/**
 * 에이전트 · 에이전트가 무엇을 했고 무엇이 막혔는가 (DESIGN-REFERENCE §4.4, AGENTIC STAR tab order:
 * 개요 · 실행 기록 · 증적 · 설정). Source: the open plan's SSE events (trial.*, tool.denied, analysis.done).
 */
import { useQuery } from '@tanstack/react-query';
import { Button } from '@astryxdesign/core/Button';
import { Selector } from '@astryxdesign/core/Selector';
import { HStack, VStack } from '@astryxdesign/core/Stack';
import { Tab, TabList } from '@astryxdesign/core/TabList';
import { Text } from '@astryxdesign/core/Text';
import type { StreamStatus } from '../api/client';
import type { PlanEvent } from '../api/types';
import { fmtTs, shortId } from '../lib/format';
import { useApi, type Session } from '../state';
import { useHashParam } from './hashQuery';
import { Block, DataTable, Facts, Screen, StatusText, Val, type Health } from './ScreenFrame';
import { NotConnected, OkDot } from './live';
import { AGENT_ROLES, foldTrials, ROLE_LABEL, verifierSummary, type Trial } from './planEvents';

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

const trialHealth = (t: Trial): Health => (t.phase !== 'verdict' ? 'run' : t.verdict === 'PASS' ? 'ok' : t.verdict === 'FAIL' ? 'bad' : 'warn');
const trialText = (t: Trial) => (t.phase === 'requested' ? '진행 중' : t.phase === 'proposed' ? '제안 · 검증 중' : t.verdict ?? '판정 없음');

function eventsCsv(events: PlanEvent[]): string {
  const esc = (v: unknown) => {
    const s = v === null || v === undefined ? '' : typeof v === 'string' ? v : JSON.stringify(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return ['id,at,kind,state,mode,actor_kind,data', ...events.map((e) => [e.id, e.at, e.kind, e.state, e.mode, e.actor.kind, e.data].map(esc).join(','))].join('\n');
}

export function AgentsScreen({ planId, events, status, detail, session, onOpenPlan }: { planId: string | null; events: PlanEvent[]; status: StreamStatus; detail: string | null; session: Session; onOpenPlan: (id: string) => void }) {
  const api = useApi();
  const token = session.user?.token ?? '';
  const [tab, setTab] = useHashParam('tab', 'overview');
  const [sel, setSel] = useHashParam('trial', '');
  const plans = useQuery({ queryKey: ['plans', token], queryFn: () => api.listPlans(token), retry: 0, staleTime: 30_000 });
  const trials = planId ? foldTrials(events) : [];
  const denied = trials.flatMap((t) => t.denied.map((d) => ({ ...d, role: t.role, trial: t.rehearsalId })));
  const picked = trials.find((t) => t.rehearsalId === sel) ?? trials[0] ?? null;
  const planOptions = (plans.data ?? []).map((p) => ({ value: p.plan_id, label: `${shortId(p.plan_id)} · ${p.workload ?? '—'} · ${p.state ?? '—'}` }));

  const download = () => {
    const url = URL.createObjectURL(new Blob([eventsCsv(events)], { type: 'text/csv;charset=utf-8' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = `bravo-agent-events-${planId ?? 'none'}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <Screen
      title="에이전트"
      lead="에이전트가 무엇을 했고 무엇이 막혔는가"
      aside={
        <HStack gap={2} vAlign="center">
          {plans.error ? (
            <span className="meta">계획 목록 연결 안 됨</span>
          ) : (
            <Selector label="계획" isLabelHidden size="sm" width={300} options={planOptions} value={planId ?? ''} onChange={(v) => v && onOpenPlan(v)} placeholder="계획 고르기…" />
          )}
          {planId && <StreamBadge status={status} detail={detail} />}
        </HStack>
      }
    >
      <TabList value={tab} onChange={setTab} aria-label="에이전트 보기">
        <Tab value="overview" label="개요" />
        <Tab value="runs" label="실행 기록" />
        <Tab value="evidence" label="증적" />
        <Tab value="settings" label="설정" />
      </TabList>

      {!planId ? (
        <Block title="계획 없음">
          <NotConnected title="계획 없음" endpoint="GET /v1/plans/{plan_id}/events (SSE)" reason="에이전트 이벤트는 계획마다 흐릅니다. 위에서 계획을 고르세요." />
        </Block>
      ) : tab === 'overview' ? (
        <>
          <Block title="역할별 에이전트">
            <DataTable
              rows={AGENT_ROLES.map((role) => {
                const mine = trials.filter((t) => t.role === role);
                return { role, last: mine[0] ?? null, n: mine.length, proposals: mine.filter((t) => t.digest).length, denied: mine.reduce((a, t) => a + t.denied.length, 0) };
              })}
              rowKey={(r) => r.role}
              empty="—"
              cols={[
                { key: 'r', header: '역할', weight: 2, render: (r) => ROLE_LABEL[r.role] },
                { key: 's', header: '상태', weight: 2, render: (r) => (r.last ? <StatusText h={trialHealth(r.last)} text={trialText(r.last)} /> : <span className="meta">아직 기록 없음</span>) },
                { key: 'n', header: '시험', numeric: true, render: (r) => r.n },
                { key: 'p', header: '제안', numeric: true, render: (r) => r.proposals },
                { key: 'd', header: '차단 호출', numeric: true, render: (r) => r.denied },
                { key: 't', header: '마지막', numeric: true, px: 180, render: (r) => <span className="meta">{r.last ? fmtTs(r.last.updatedAt) : '—'}</span> },
              ]}
            />
          </Block>
          <Block title={`도구 호출 · 차단 ${denied.length}건`}>
            <DataTable
              rows={denied.slice().reverse()}
              rowKey={(d) => d.eventId}
              empty="차단된 도구 호출이 없습니다."
              cols={[
                { key: 't', header: '시각', px: 180, render: (d) => <span className="meta">{fmtTs(d.at)}</span> },
                { key: 'x', header: '실행', render: (d) => <span className="mono">{shortId(d.trial)}</span> },
                { key: 'r', header: '역할', render: (d) => d.role ?? '—' },
                { key: 'tool', header: '도구', weight: 2, render: (d) => <span className="mono">{d.tool ?? '—'}</span> },
                { key: 'v', header: '판정', render: () => <StatusText h="bad" text="차단" /> },
                { key: 'rule', header: '근거 규칙', weight: 2, render: (d) => <span className="mono">{d.rule ?? '—'}</span> },
              ]}
            />
            <p className="nc">
              <span className="nc__dot" aria-hidden="true">○</span>
              <b className="nc__title">허용 호출 연결 안 됨</b>
              <span>· 이 화면은 계획 이벤트 중 tool.denied만 받습니다(tool.allowed 이벤트 없음)</span>
            </p>
          </Block>
        </>
      ) : tab === 'runs' ? (
        <>
          <Block title={`실행 기록 ${trials.length}건`}>
            <DataTable
              rows={trials}
              rowKey={(t) => t.rehearsalId}
              empty="아직 기록 없음 · 이 계획에는 시험(trial.started)이 없습니다."
              cols={[
                {
                  key: 'id', header: '실행',
                  render: (t) => (
                    <button type="button" className="rowbtn mono" aria-current={picked?.rehearsalId === t.rehearsalId} onClick={() => setSel(t.rehearsalId)}>
                      {shortId(t.rehearsalId)}
                    </button>
                  ),
                },
                { key: 'r', header: '역할', render: (t) => t.role ?? '—' },
                { key: 'a', header: '시도', numeric: true, render: (t) => <Val v={t.attempt} /> },
                { key: 's', header: '상태', weight: 2, render: (t) => <StatusText h={trialHealth(t)} text={trialText(t)} /> },
                { key: 't', header: '시작', numeric: true, px: 180, render: (t) => <span className="meta">{fmtTs(t.startedAt)}</span> },
              ]}
            />
          </Block>
          {picked && (
            <Block title={`실행 기록(実行記録) · ${shortId(picked.rehearsalId)}`}>
              <Facts
                columns="single"
                items={[
                  { label: '요청', value: <span className="mono">{picked.task ?? '—'}{picked.role ? ` · ${picked.role}` : ''}{picked.attempt ? ` · 시도 ${picked.attempt}` : ''}</span> },
                  { label: '근거', value: picked.denied.length ? <span className="mono">{[...new Set(picked.denied.map((d) => d.rule ?? '—'))].join(', ')}</span> : <span className="meta">이벤트에 근거 규칙 없음(차단 호출 없음)</span> },
                  { label: '행동', value: picked.denied.length ? <span className="mono">차단 {picked.denied.length}건: {picked.denied.map((d) => d.tool ?? '—').join(', ')}</span> : <span className="meta">허용 호출은 이벤트로 오지 않음</span> },
                  { label: '검토', value: <span className="mono">{verifierSummary(picked.verifier)}</span> },
                  { label: '결과', value: <VStack gap={0}><StatusText h={trialHealth(picked)} text={trialText(picked)} />{picked.reason && <span className="mono meta">{picked.reason}</span>}{picked.digest && <span className="mono meta">제안 digest {shortId(picked.digest, 16)}</span>}</VStack> },
                ]}
              />
            </Block>
          )}
        </>
      ) : tab === 'evidence' ? (
        <Block title={`증적 · 계획 이벤트 ${events.length}건`} meta={<Button label="CSV 내보내기" size="sm" variant="secondary" clickAction={download} isDisabled={events.length === 0} />}>
          <DataTable
            rows={events.slice().reverse()}
            rowKey={(e) => e.id}
            empty="아직 기록 없음 · 이벤트가 오면 여기에 쌓입니다."
            cols={[
              { key: 't', header: '시각', px: 180, render: (e) => <span className="meta">{fmtTs(e.at)}</span> },
              { key: 'k', header: '종류', render: (e) => <span className="mono">{e.kind}</span> },
              { key: 's', header: '상태', render: (e) => <span className="mono">{e.state}</span> },
              { key: 'a', header: '주체', render: (e) => <span className="mono">{e.actor.kind}</span> },
              { key: 'm', header: '모드', render: (e) => e.mode },
            ]}
          />
        </Block>
      ) : (
        <Block title="설정 · 실행 규칙">
          <NotConnected title="실행 규칙 연결 안 됨" endpoint="허용 목록 · 테넌트 규칙 읽기 경로" reason="이 콘솔은 에이전트 허용 목록과 테넌트 실행 규칙을 읽는 출처가 없습니다. 차단 판정의 근거 규칙 이름은 개요 탭의 도구 호출 표에 있습니다" />
        </Block>
      )}
    </Screen>
  );
}
