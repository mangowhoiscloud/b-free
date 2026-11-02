import type { ConsoleView } from '../api/derive';
import { ALERT_STATES, MAIN_STATES, OUT_OF_SCOPE_STATES, type MainState, type PlanState } from '../api/types';
import { IconAlert, ModeBadge } from './bits';

const LABEL: Record<PlanState, string> = {
  COLLECT: '수집',
  ANALYZE: '분석',
  PROPOSE: '제안',
  REHEARSE: '리허설',
  GATE: '게이트',
  AWAIT_APPROVAL: '승인 대기',
  CUTOVER: '컷오버',
  WATCH: '관찰',
  SAFE_HOLD: '안전 정지',
  ABORT_PRE_WRITE: '쓰기 전 중단',
};

const ALERT_HELP: Record<string, string> = {
  ABORT_PRE_WRITE: '6a 단계 전에 실패해 원복했습니다. 다시 승인받은 뒤 시작합니다.',
  SAFE_HOLD: '6a 단계 뒤에 실패해 멈췄습니다. 자동으로 되돌리지 않습니다. 사람이 판단합니다.',
};

export function stateLabel(s: PlanState): string {
  return LABEL[s];
}

/** Index of the last main state reached; for alert states, the step they interrupted. */
function currentIndex(view: ConsoleView): number {
  const i = (MAIN_STATES as readonly string[]).indexOf(view.state);
  if (i >= 0) return i;
  if (view.state === 'ABORT_PRE_WRITE' || view.state === 'SAFE_HOLD') return MAIN_STATES.indexOf('CUTOVER');
  return 0;
}

export function StateProgress({ view }: { view: ConsoleView }) {
  const cur = currentIndex(view);
  const interrupted = view.alert !== null;
  return (
    <nav className="progress" aria-label="상태 머신 진행">
      <ol className="progress__steps">
        {MAIN_STATES.map((s: MainState, i) => {
          const skipped = OUT_OF_SCOPE_STATES.includes(s) && view.state !== s;
          const status = skipped
            ? 'skip'
            : i < cur
              ? 'done'
              : i === cur
                ? interrupted
                  ? 'halted'
                  : 'current'
                : 'todo';
          const mode = view.modes[s];
          const statusText =
            status === 'skip'
              ? '이 계획은 들어가지 않음'
              : status === 'done'
                ? '지남'
                : status === 'current'
                  ? '현재'
                  : status === 'halted'
                    ? '중단됨'
                    : '아직';
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
                {status === 'skip' ? (
                  <span className="step__skip" aria-hidden="true">
                    들어가지 않음
                  </span>
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
