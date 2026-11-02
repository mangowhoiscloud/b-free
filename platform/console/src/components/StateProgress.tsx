import type { ConsoleView } from '../api/derive';
import { ALERT_STATES, MAIN_STATES, SIDE_STATES, type MainState, type PlanState } from '../api/types';
import { IconAlert, ModeBadge } from './bits';

const LABEL: Record<PlanState, string> = {
  COLLECT: '수집',
  ANALYZE: '분석',
  PROPOSE: '제안',
  REHEARSE: '리허설',
  GATE: '게이트',
  AWAIT_APPROVAL: '승인 대기',
  SHIFT: '전환 10/50/100',
  CUTOVER: '컷오버',
  WATCH: '관찰',
  DONE: '끝',
  FAILBACK: '되돌리기',
  DONE_ROLLED_BACK: '되돌리기 끝',
  NEEDS_HUMAN: '사람 판단 필요',
  SAFE_HOLD: '안전 정지',
  ABORT_PRE_WRITE: '쓰기 전 중단',
};

const ALERT_HELP: Record<string, string> = {
  ABORT_PRE_WRITE: '6a 단계 전에 실패해 원복했습니다. 다시 승인받은 뒤 시작합니다.',
  SAFE_HOLD: '6a 단계 뒤에 실패해 멈췄습니다. 자동으로 되돌리지 않습니다. 사람이 판단합니다.',
};

/** Why a plan holds in its current state (state_reason). ANALYZE holds when the inventory could not be read (audit A4). */
export function reasonText(reason: string): string {
  if (reason === 'collector_unconfigured') return '수집기가 설정되지 않아 의존성 인벤토리를 읽지 못함(BRAVO_COLLECTOR_URL 없음)';
  if (reason === 'inventory_root_empty') return '수집기에 검사할 앱 폴더가 연결되지 않음(인벤토리 대상 없음)';
  if (reason.startsWith('collector_')) return `수집기에서 인벤토리를 읽지 못함(${reason})`;
  if (reason.startsWith('environment_secret_unverified')) return `환경 비밀 참조를 확인하지 못함(${reason})`;
  return reason;
}

export function stateLabel(s: PlanState): string {
  return LABEL[s];
}

/** Index of the last main state reached; for alert states, the step they interrupted. */
function currentIndex(view: ConsoleView): number {
  const i = (MAIN_STATES as readonly string[]).indexOf(view.state);
  if (i >= 0) return i;
  if (view.state === 'ABORT_PRE_WRITE' || view.state === 'SAFE_HOLD') return MAIN_STATES.indexOf('CUTOVER');
  // Side states: failback runs after WATCH; NEEDS_HUMAN stops the rehearsal loop.
  if (view.state === 'FAILBACK' || view.state === 'DONE_ROLLED_BACK') return MAIN_STATES.indexOf('WATCH');
  if (view.state === 'NEEDS_HUMAN') return MAIN_STATES.indexOf('REHEARSE');
  return 0;
}

export function StateProgress({ view }: { view: ConsoleView }) {
  const cur = currentIndex(view);
  const side = (SIDE_STATES as readonly string[]).includes(view.state) ? view.state : null;
  const interrupted = view.alert !== null;
  return (
    <nav className="progress" aria-label="상태 머신 진행">
      <ol className="progress__steps">
        {MAIN_STATES.map((s: MainState, i) => {
          // A state_reason on the current step means the plan is held there, not done (e.g. ANALYZE without inventory).
          const held = i === cur && !interrupted && Boolean(view.stateReason);
          // A step before the current one counts as passed only when the plan was seen in it (events or snapshot).
          // ANALYZE without gaps goes straight to GATE, so PROPOSE and REHEARSE may never be entered.
          const seen = view.reached.has(s) || Boolean(view.modes[s]);
          const status = i < cur ? (seen ? 'done' : 'skip') : i === cur ? (interrupted || held ? 'halted' : 'current') : 'todo';
          const mode = view.modes[s];
          const statusText = status === 'done' ? '지남' : status === 'skip' ? '들어간 기록 없음' : status === 'current' ? '현재' : status === 'halted' ? (held ? '멈춤' : '중단됨') : '아직';
          return (
            <li key={s} className={`step step--${status}`} aria-current={status === 'current' ? 'step' : undefined}>
              <span className="step__num" aria-hidden="true">
                {String(i + 1).padStart(2, '0')}
              </span>
              <span className="step__name">{s}</span>
              <span className="step__label">
                {LABEL[s]}
                <span className="sr-only">, {statusText}</span>
              </span>
              <span className="step__mode">
                {held ? (
                  <span className="step__skip">멈춤</span>
                ) : status === 'skip' ? (
                  <span className="step__skip">기록 없음</span>
                ) : mode ? (
                  <ModeBadge mode={mode} />
                ) : (
                  <span className="step__none">이벤트 없음</span>
                )}
              </span>
            </li>
          );
        })}
      </ol>
      {side && (
        <p className="reason-codes" role="status">
          <span className="muted">본선 밖 상태</span> <code>{side}</code> {LABEL[side as PlanState]}
          {side === 'DONE_ROLLED_BACK' && view.stateReason === 'front_not_moved' && <span className="muted"> · DB는 되돌렸지만 앞단은 옮기지 못함(front_not_moved)</span>}
        </p>
      )}
      <ul className="progress__alerts" aria-label="경보 상태">
        {ALERT_STATES.map((s) => {
          const active = view.state === s;
          return (
            <li key={s} className={`alert-chip${active ? ' alert-chip--on' : ''}`}>
              <IconAlert size={14} />
              <span className="alert-chip__name">{s}</span>
              <span className="alert-chip__label">{active ? `${LABEL[s]} · 지금` : LABEL[s]}</span>
            </li>
          );
        })}
      </ul>
      {!view.alert && view.stateReason && (
        <div className="alert-banner" role="status">
          <IconAlert size={22} />
          <div>
            <strong>
              {view.state} · {LABEL[view.state]}에 머묾
            </strong>
            <p>
              {reasonText(view.stateReason)}. 이 단계를 끝난 것으로 보지 않습니다. 원인을 고치면 다음 단계로 갑니다. <span className="mono">state_reason={view.stateReason}</span>
            </p>
          </div>
        </div>
      )}
      {view.alert && (
        <div className="alert-banner" role="alert">
          <IconAlert size={22} />
          <div>
            <strong>
              {view.alert} · {LABEL[view.alert]}
            </strong>
            <p>{ALERT_HELP[view.alert]}</p>
          </div>
        </div>
      )}
    </nav>
  );
}
