import { useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { AppShell } from '@astryxdesign/core/AppShell';
import { SideNav, SideNavItem, SideNavSection } from '@astryxdesign/core/SideNav';
import { AgentsScreen } from './live/AgentsScreen';
import { ApprovalsScreen } from './live/ApprovalsScreen';
import { CostScreen } from './live/CostScreen';
import { EndingScreen } from './live/EndingScreen';
import { HomeScreen } from './live/HomeScreen';
import { ClustersScreen } from './live/ClustersScreen';
import { DemoAppScreen } from './live/DemoAppScreen';
import { ObservabilityScreen } from './live/ObservabilityScreen';
import { WorkflowScreen } from './live/WorkflowScreen';
import { errorText } from './api/client';
import { deriveView } from './api/derive';
import type { PlanAnalysis } from './api/types';
import { ApprovalPanel } from './components/ApprovalPanel';
import { ExceptionsPanel } from './components/ExceptionsPanel';
import { ErrorLine, Panel } from './components/bits';
import { CutoverPanel } from './components/CutoverPanel';
import { EventStream } from './components/EventStream';
import { GatePanel } from './components/GatePanel';
import { Header, PlanPicker } from './components/Header';
import { reasonText, StateProgress } from './components/StateProgress';
import { useApi, usePlanParam, usePlanStream, useSession } from './state';

const SCREENS = [
  { id: 'home', label: '홈', group: '개요' },
  { id: 'plan', label: '계획 · 승인', group: '이전' },
  { id: 'approvals', label: '승인 기록', group: '이전' },
  { id: 'workflow', label: '워크플로우', group: '이전' },
  { id: 'cost', label: '비용 영수증', group: '이전' },
  { id: 'clusters', label: '클러스터', group: '환경' },
  { id: 'demo', label: '데모 앱', group: '환경' },
  { id: 'observability', label: '관측', group: '관측' },
  { id: 'agents', label: '에이전트', group: '관측' },
  { id: 'ending', label: '마무리', group: '증거' },
] as const;
const GROUPS = ['개요', '이전', '환경', '관측', '증거'] as const;
const ALIASES: Record<string, string> = { observe: 'observability' };
type ScreenId = (typeof SCREENS)[number]['id'];

const screenFromHash = (): ScreenId => {
  const raw = window.location.hash.replace(/^#\/?/, '').split('?')[0] ?? '';
  const h = ALIASES[raw] ?? raw;
  const found = SCREENS.find((s) => s.id === h)?.id;
  if (found) return found;
  // No screen in the hash: a plan URL (/plans/<id>, or #approve) opens the plan; anything else opens home.
  return (/\/plans\/[^/]+\/?$/.test(window.location.pathname) || new URLSearchParams(window.location.search).has('plan') ? 'plan' : 'home') as ScreenId;
};

/** Screen in the URL hash, so it survives the plan path changes made with pushState. */
function useScreen(): ScreenId {
  const [screen, setScreen] = useState<ScreenId>(screenFromHash);
  useEffect(() => {
    const on = () => setScreen(screenFromHash());
    window.addEventListener('hashchange', on);
    return () => window.removeEventListener('hashchange', on);
  }, []);
  useEffect(() => {
    const raw = window.location.hash.replace(/^#\/?/, '');
    if ((raw.split('?')[0] ?? '') !== screen && screenFromHash() !== screen) history.replaceState(null, '', `${window.location.pathname}${window.location.search}#/${screen}`);
    else if (ALIASES[raw.split('?')[0] ?? '']) history.replaceState(null, '', `${window.location.pathname}${window.location.search}#/${screen}${raw.includes('?') ? `?${raw.split('?')[1]}` : ''}`);
  });
  return screen;
}

export function App() {
  const screen = useScreen();
  const api = useApi();
  const session = useSession();
  const [planId, setPlanId] = usePlanParam();
  const token = session.user?.token ?? null;
  const stream = usePlanStream(planId, token);

  const planQ = useQuery({
    queryKey: ['plan', planId, token],
    enabled: Boolean(planId),
    queryFn: async () => {
      const seen = stream.countRef.current;
      const plan = await api.getPlan(token ?? '', planId ?? '');
      return { plan, seen };
    },
    refetchInterval: 15_000,
  });
  // Environment names for the plan's source/target ids (gate report and exception evidence need names).
  const envsQ = useQuery({ queryKey: ['environments', token ?? ''], queryFn: () => api.listEnvironments(token ?? ''), enabled: Boolean(planId), retry: 0, staleTime: 60_000 });
  const envName = (id: string | null | undefined) => (id ? envsQ.data?.find((e) => e.id === id)?.name ?? null : null);
  const planNames = { source: envName(planQ.data?.plan.source), target: envName(planQ.data?.plan.target) };

  const view = useMemo(
    () => (planId ? deriveView(planId, planQ.data?.plan, stream.events, planQ.data?.seen ?? 0) : null),
    [planId, planQ.data, stream.events],
  );

  const nav = (
    <SideNav
      header={
        <div className="shell-brand">
          <span className="shell-brand__mark">BRAVO</span>
          <span className="shell-brand__name">이전 플랫폼 콘솔</span>
        </div>
      }
      footer={<div className="shell-foot">시각 표시 UTC+9 (KST · JST)</div>}
    >
      {GROUPS.map((g) => (
        <SideNavSection key={g} title={g}>
          {SCREENS.filter((s) => s.group === g).map((s) => (
            <SideNavItem
              key={s.id}
              label={s.label}
              href={`#/${s.id}`}
              isSelected={screen === s.id}
            />
          ))}
        </SideNavSection>
      ))}
    </SideNav>
  );

  const events = stream.events;
  return (
    <>
    {/* Korean skip link; the Astryx AppShell one is English-only and hidden in astryx-bravo.css. */}
    <a className="skip" href="#content">
      본문으로 건너뛰기
    </a>
    <AppShell sideNav={nav} height="auto" variant="section" contentPadding={0}>
      <div className="app" id="content" tabIndex={-1}>
      <Header session={session} />
      {screen !== 'plan' && (
        <div className="screen-wrap">
          {screen === 'agents' && <AgentsScreen planId={planId} events={events} status={stream.status} detail={stream.detail} session={session} onOpenPlan={setPlanId} />}
          {screen === 'clusters' && <ClustersScreen />}
          {screen === 'workflow' && <WorkflowScreen planId={planId} view={view} events={events} status={stream.status} detail={stream.detail} />}
          {screen === 'demo' && <DemoAppScreen view={view} />}
          {screen === 'observability' && <ObservabilityScreen view={view} planId={planId} events={events} />}
          {screen === 'home' && (
            <HomeScreen
              session={session}
              onOpenPlan={(id) => {
                setPlanId(id);
                window.location.hash = '/plan';
              }}
            />
          )}
          {screen === 'approvals' && <ApprovalsScreen planId={planId} view={view} events={events} status={stream.status} detail={stream.detail} user={session.user} names={planNames} />}
          {screen === 'ending' && <EndingScreen />}
          {screen === 'cost' && <CostScreen session={session} />}
        </div>
      )}
      {screen === 'plan' && (
      <div id="main" className="main" tabIndex={-1}>
        <PlanPicker planId={planId} onOpen={setPlanId} session={session} />
        {!planId || !view ? (
          <section className="welcome" aria-labelledby="welcome-title">
            <h2 id="welcome-title">열린 계획이 없습니다</h2>
            <p>
              <a href="#/home">홈</a>의 <b>이전 계획</b> 표에서 계획을 고르거나, 운영자라면 <b>새 이전 시작</b>으로 만듭니다. MCP <code>plan_get</code>이 돌려준
              콘솔 주소(<code>…/plans/&lt;plan_id&gt;</code>)로 열어도 됩니다.
            </p>
          </section>
        ) : (
          <>
            <div className="plan-line">
              <span className="plan-line__k">계획</span>
              <span className="mono plan-line__id">{planId}</span>
              {planQ.data?.plan.workload && <span className="plan-line__meta">워크로드 {planQ.data.plan.workload}</span>}
              {view.createdBy && <span className="plan-line__meta">작성 {view.createdBy}</span>}
              {view.stateReason && <span className="plan-line__meta mono">사유 {view.stateReason}</span>}
              {planQ.data?.plan.revision && <span className="plan-line__meta mono">rev {planQ.data.plan.revision}</span>}
              {session.user && (
                <span className="plan-line__me">
                  지금 사용자 <b>{session.user.email}</b> ({session.user.role})
                </span>
              )}
            </div>
            <ErrorLine text={planQ.error ? `계획을 읽지 못했습니다. ${errorText(planQ.error)}` : null} />
            <StateProgress view={view} />
            <div className="grid">
              <div className="col col--main">
                <GatePanel planId={planId} view={view} user={session.user} />
                <DependencyPanel analysis={planQ.data?.plan.analysis ?? null} state={view.state} stateReason={view.stateReason} />
              </div>
              <div className="col col--side">
                <ApprovalPanel planId={planId} view={view} user={session.user} />
                <ExceptionsPanel planId={planId} view={view} user={session.user} names={planNames} />
                <CutoverPanel planId={planId} view={view} user={session.user} />
              </div>
              <div className="col col--events">
                <EventStream events={stream.events} status={stream.status} detail={stream.detail} />
              </div>
            </div>
          </>
        )}
      </div>
      )}
      </div>
    </AppShell>
    </>
  );
}

const GRADE_TEXT: Record<string, string> = { config_swap: '설정 교체', adapter: '어댑터 추가', code_change: '코드 수정', no_change: '이전 불필요', unclassified: '미분류' };

/** ANALYZE result from plan.analysis (audit A4): inventory status first; a missing inventory is never "no gaps". */
function DependencyPanel({ analysis, state, stateReason }: { analysis: PlanAnalysis | null; state: string; stateReason: string | null }) {
  const inv = analysis?.inventoryStatus ?? null;
  return (
    <Panel id="deps" step="분석" title="의존성 분석 (ANALYZE)" className="panel--deps">
      {!analysis ? (
        <p className="empty">
          {state === 'COLLECT' ? '아직 분석 전입니다(COLLECT 단계).' : '계획 응답에 분석 결과(analysis)가 없습니다.'}
          {stateReason ? ` state_reason=${stateReason}` : ''}
        </p>
      ) : inv !== 'ok' ? (
        <p className="empty" role="status">
          ○ 분석 안 됨 · 인벤토리 {inv ?? '상태 없음'} · {analysis.inventoryReason ? reasonText(analysis.inventoryReason) : '이유 없음'}. 의존성 공백이 없다고 보지 않고 ANALYZE에 머뭅니다.
        </p>
      ) : (
        <>
          <ul className="deps">
            {Object.entries(analysis.byGrade).map(([k, v]) => (
              <li key={k} className="deps__item">
                <span className="deps__num mono">{v}</span>
                <span className="deps__label">{GRADE_TEXT[k] ?? k}</span>
              </li>
            ))}
          </ul>
          <p className="muted">
            공백(코드 수정 · 미분류) {analysis.gaps}건 · 통제 차이 {analysis.controlDifferences.length ? analysis.controlDifferences.join(', ') : '없음'} · 파일 {analysis.filesScanned ?? '—'}개 · 분석 {analysis.analyzedAt ?? '—'}
          </p>
        </>
      )}
    </Panel>
  );
}
