import { useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { AppShell } from '@astryxdesign/core/AppShell';
import { SideNav, SideNavItem, SideNavSection } from '@astryxdesign/core/SideNav';
import { AgentsScreen } from './live/AgentsScreen';
import { ApprovalsScreen } from './live/ApprovalsScreen';
import { EndingScreen } from './live/EndingScreen';
import { ClustersScreen } from './live/ClustersScreen';
import { DemoAppScreen } from './live/DemoAppScreen';
import { ObservabilityScreen } from './live/ObservabilityScreen';
import { WorkflowScreen } from './live/WorkflowScreen';
import { errorText } from './api/client';
import { deriveView } from './api/derive';
import type { DependencyCounts } from './api/types';
import { ApprovalPanel } from './components/ApprovalPanel';
import { ErrorLine, Panel } from './components/bits';
import { CutoverPanel } from './components/CutoverPanel';
import { EventStream } from './components/EventStream';
import { GatePanel } from './components/GatePanel';
import { Header } from './components/Header';
import { StateProgress } from './components/StateProgress';
import { useApi, usePlanParam, usePlanStream, useSession } from './state';

const SCREENS = [
  { id: 'plan', label: '계획 · 승인 콘솔', group: 'operate' },
  { id: 'approvals', label: '승인 (change · cutover · failback)', group: 'operate' },
  { id: 'agents', label: '에이전트 실시간', group: 'watch' },
  { id: 'clusters', label: '클러스터 실시간', group: 'watch' },
  { id: 'workflow', label: 'CI/CD · 워크플로우', group: 'watch' },
  { id: 'demo', label: '데모 앱 상태', group: 'watch' },
  { id: 'observe', label: '관측', group: 'watch' },
  { id: 'ending', label: '마무리 장면', group: 'watch' },
] as const;
type ScreenId = (typeof SCREENS)[number]['id'];

const screenFromHash = (): ScreenId => {
  const h = window.location.hash.replace(/^#\/?/, '');
  return (SCREENS.find((s) => s.id === h)?.id ?? 'plan') as ScreenId;
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
    if (screenFromHash() !== screen) history.replaceState(null, '', `${window.location.pathname}${window.location.search}#/${screen}`);
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
    enabled: Boolean(planId && token),
    queryFn: async () => {
      const seen = stream.countRef.current;
      const plan = await api.getPlan(token ?? '', planId ?? '');
      return { plan, seen };
    },
    refetchInterval: 15_000,
  });

  const view = useMemo(
    () => (planId ? deriveView(planId, planQ.data?.plan, stream.events, planQ.data?.seen ?? 0) : null),
    [planId, planQ.data, stream.events],
  );

  const nav = (
    <SideNav
      header={
        <div className="shell-brand">
          <span className="shell-brand__mark">BRAVO</span>
          <span className="shell-brand__name">플랫폼 실시간 콘솔</span>
        </div>
      }
      footer={<div className="shell-foot">읽기 전용 화면 · 승인은 콘솔에서만</div>}
    >
      {(['operate', 'watch'] as const).map((g) => (
        <SideNavSection key={g} title={g === 'operate' ? '운영' : '실시간 보기'}>
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
      <Header session={session} planId={planId} onOpenPlan={setPlanId} />
      {screen !== 'plan' && (
        <div className="screen-wrap">
          {screen === 'agents' && <AgentsScreen planId={planId} events={events} status={stream.status} detail={stream.detail} />}
          {screen === 'clusters' && <ClustersScreen />}
          {screen === 'workflow' && <WorkflowScreen planId={planId} view={view} events={events} status={stream.status} detail={stream.detail} />}
          {screen === 'demo' && <DemoAppScreen view={view} />}
          {screen === 'observe' && <ObservabilityScreen />}
          {screen === 'approvals' && <ApprovalsScreen planId={planId} view={view} events={events} status={stream.status} detail={stream.detail} user={session.user} />}
          {screen === 'ending' && <EndingScreen />}
        </div>
      )}
      {screen === 'plan' && (
      <div id="main" className="main" tabIndex={-1}>
        {!planId || !view ? (
          <section className="welcome" aria-labelledby="welcome-title">
            <h2 id="welcome-title">계획을 여세요</h2>
            <p>
              위의 <b>계획 ID</b> 칸에 MCP <code>plan_create</code>가 돌려준 ID를 붙여넣거나, operator로 <b>새 계획</b>을 만듭니다.
              MCP <code>plan_get</code>이 돌려준 콘솔 주소(<code>…/plans/&lt;plan_id&gt;</code>)로 열어도 됩니다.
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
                <DependencyPanel deps={planQ.data?.plan.dependencies ?? null} />
              </div>
              <div className="col col--side">
                <ApprovalPanel planId={planId} view={view} user={session.user} />
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

function DependencyPanel({ deps }: { deps: DependencyCounts | null }) {
  const rows: { k: keyof DependencyCounts; label: string; help: string }[] = [
    { k: 'config', label: '설정 교체', help: '접속 문자열 같은 설정만 바꿈' },
    { k: 'adapter', label: '어댑터 추가', help: '코드는 두고 어댑터를 더함' },
    { k: 'code', label: '코드 수정', help: '앱 코드를 고쳐야 함' },
  ];
  return (
    <Panel id="deps" step="분석" title="의존성 3등급" className="panel--deps">
      {deps ? (
        <ul className="deps">
          {rows.map((r) => (
            <li key={r.k} className="deps__item">
              <span className="deps__num mono">{deps[r.k] ?? '—'}</span>
              <span className="deps__label">{r.label}</span>
              <span className="deps__help muted">{r.help}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="empty">계획 응답에 의존성 목록이 없습니다. ANALYZE는 파일럿 범위 밖입니다.</p>
      )}
    </Panel>
  );
}
