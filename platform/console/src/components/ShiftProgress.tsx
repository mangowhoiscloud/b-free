/**
 * SHIFT 10/50/100 progress (contract docs/architecture/15-r2-contracts.md §10).
 * Each step is judged on real chat writes the destination answered: at least 100 samples, error rate ≤ 1 %,
 * p95 ≤ 800 ms (Executor defaults, §10.4; the run's own reasons are shown as sent). The decision and its
 * reasons come from the run; nothing here is computed into a verdict.
 */
import type { ShiftStep, ShiftSummary } from '../api/types';
import { fmtTs, shortId } from '../lib/format';
import { Mark, type Tone } from './bits';

/** Executor defaults (§10.4). Shown as the reference line; the Executor's own reasons decide. */
export const SHIFT_RULES = { minSamples: 100, maxErrorRate: 0.01, maxP95Ms: 800 } as const;
const WEIGHTS = [10, 50, 100];

const pct = (v: number | null) => (v === null ? '—' : `${(v * 100).toFixed(1)} %`);
const ms = (v: number | null) => (v === null ? '—' : v < 1000 ? `${Math.round(v)} ms` : `${(v / 1000).toFixed(2)} s`);

function Check({ ok, children }: { ok: boolean | null; children: React.ReactNode }) {
  return <Mark tone={ok === null ? 'unknown' : ok ? 'pass' : 'block'}>{children}</Mark>;
}

function StepRow({ weight, step, current }: { weight: number; step: ShiftStep | undefined; current: boolean }) {
  if (!step) {
    return (
      <tr className={current ? 'row--current' : undefined}>
        <th scope="row" className="num">{weight} %</th>
        <td colSpan={5} className="muted">
          {current ? '가중치 반영됨 · 표본을 모으는 중…' : '아직'}
        </td>
      </tr>
    );
  }
  const okSamples = step.target_samples === null ? null : step.target_samples >= SHIFT_RULES.minSamples;
  const okErr = step.target_error_rate === null ? null : step.target_error_rate <= SHIFT_RULES.maxErrorRate;
  const okP95 = step.target_p95_ms === null ? null : step.target_p95_ms <= SHIFT_RULES.maxP95Ms;
  return (
    <tr className={step.healthy === false ? 'row--block' : undefined}>
      <th scope="row" className="num">{weight} %</th>
      <td>
        <Check ok={okSamples}>{step.target_samples ?? '—'}건</Check>
        <span className="cell-sub">전체 {step.samples ?? '—'} · {Object.entries(step.by_env).map(([k, n]) => `${k} ${n}`).join(' · ') || '—'}</span>
      </td>
      <td>
        <Check ok={okErr}>{pct(step.target_error_rate)}</Check>
        <span className="cell-sub">오류 {step.target_errors ?? '—'}건 · 출발지 {pct(step.error_rate)}</span>
      </td>
      <td>
        <Check ok={okP95}>{ms(step.target_p95_ms)}</Check>
        <span className="cell-sub">출발지 {ms(step.p95_ms)}</span>
      </td>
      <td className="num mono">{ms(step.hold_ms)}</td>
      <td>
        <Mark tone={step.healthy === null ? 'unknown' : step.healthy ? 'pass' : 'block'}>{step.healthy === null ? '판정 없음' : step.healthy ? '통과' : '기준 넘음'}</Mark>
        {step.reasons.length > 0 && <span className="cell-sub mono">{step.reasons.join(' · ')}</span>}
      </td>
    </tr>
  );
}

export function ShiftProgress({ shift }: { shift: ShiftSummary }) {
  const decision: { tone: Tone; text: string; why: string } =
    shift.reverted
      ? { tone: 'block', text: `${shift.reverted.at_weight ?? '—'} %에서 0 %로 복귀(1a)`, why: shift.reverted.reasons.join(' · ') || '이유 없음' }
      : shift.status === 'DONE'
        ? { tone: 'pass', text: '세 단계 통과 · 목적지 100 %', why: '같은 승인으로 시스템이 DB 컷오버를 시작합니다.' }
        : shift.status === 'RUNNING'
          ? { tone: 'unknown', text: `진행 중 · 지금 ${shift.current_weight ?? '—'} %`, why: '단계마다 목적지가 받은 실제 채팅 쓰기로 판정합니다.' }
          : { tone: 'block', text: shift.status, why: '실행 기록을 보세요.' };
  return (
    <section className="shift" aria-label="전환 10/50/100">
      <div className="shift__head">
        <h3 className="receipt__title">전환 10 → 50 → 100</h3>
        <Mark tone={decision.tone} size="lg">
          {decision.text}
        </Mark>
        <span className="mono muted" title={shift.run_id}>
          shift {shortId(shift.run_id, 10)} · 대상 {shift.target_env ?? '—'} · 시작 {fmtTs(shift.started_at)}
        </span>
      </div>
      <p className="muted">판정: {decision.why}</p>
      <div className="weightbar" role="img" aria-label={`목적지 가중치 ${shift.current_weight ?? 0} %`}>
        <span className="weightbar__fill" style={{ width: `${Math.max(0, Math.min(100, shift.current_weight ?? 0))}%` }} />
        <span className="weightbar__label">목적지 {shift.current_weight ?? '—'} %</span>
      </div>
      <div className="table-wrap" tabIndex={0} role="region" aria-label="단계별 판정">
        <table className="table">
          <caption className="sr-only">가중치 단계마다 목적지 표본, 오류율, p95, 보유 시간, 판정</caption>
          <thead>
            <tr>
              <th scope="col">가중치</th>
              <th scope="col">목적지 표본 <span className="th-sub">≥ {SHIFT_RULES.minSamples}건</span></th>
              <th scope="col">목적지 오류율 <span className="th-sub">≤ 1 %</span></th>
              <th scope="col">목적지 p95 <span className="th-sub">≤ {SHIFT_RULES.maxP95Ms} ms</span></th>
              <th scope="col" className="num">보유</th>
              <th scope="col">단계 판정</th>
            </tr>
          </thead>
          <tbody>
            {WEIGHTS.map((w) => (
              <StepRow key={w} weight={w} step={shift.steps.find((s) => s.weight === w)} current={shift.status === 'RUNNING' && shift.current_weight === w} />
            ))}
          </tbody>
        </table>
      </div>
      <p className="footnote">기준은 Executor 기본값(계약 15 §10.4)입니다. 단계 판정과 이유는 실행이 보낸 그대로이고, 표본은 대표 이름으로 들어온 실제 채팅 쓰기(방 shift-probe)입니다.</p>
    </section>
  );
}
