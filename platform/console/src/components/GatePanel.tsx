import { Fragment, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { ApiError, errorText } from '../api/client';
import type { ConsoleView } from '../api/derive';
import type { ControlException, ControlRow, DevToken, GateResult } from '../api/types';
import { fmtDateTime, fmtTs } from '../lib/format';
import { idemKey, releaseIdemKey } from '../lib/idem';
import { useApi } from '../state';
import { ActorBadge, ControlValueMark, ErrorLine, Mark, Panel, Reasons } from './bits';
import { EXCEPTION_STATUS, exceptionsError, useExceptions } from './ExceptionsPanel';

/** Plan states in which the API refuses exception requests (contract 15 §9.3 state_mismatch). */
const EXCEPTION_LOCKED = new Set(['CUTOVER', 'FAILBACK', 'DONE', 'DONE_ROLLED_BACK', 'SAFE_HOLD', 'ABORT_PRE_WRITE', 'WATCH']);

/** A row can be excepted only when it is a measured destination FAIL with evidence (contract 15 §9.1). */
function exceptable(c: ControlRow): boolean {
  return c.verdict === 'BLOCK' && c.dst === 'FAIL' && (c.src === 'PASS' || c.src === 'FAIL') && c.evidence_digest !== null;
}

/** Evidence validity of a verdict (contract 15 §9.1): past `evidence_expires_at`, or without one (the gate
 *  report was not read), nothing can be excepted — the API refuses with exception_stale/evidence_expired. */
export function evidenceBlock(gate: { evidence_expires_at: string | null }): string | null {
  if (gate.evidence_expires_at === null) return '이 판정에는 증거 유효기간이 없어(게이트 보고서를 읽지 못한 판정) 예외를 걸 수 없습니다. 게이트를 다시 돌립니다.';
  if (Date.parse(gate.evidence_expires_at) <= Date.now()) return `이 판정의 증거가 만료되었습니다(${fmtTs(gate.evidence_expires_at)}). 만료된 증거로는 예외를 걸 수 없습니다. 게이트를 다시 돌립니다.`;
  return null;
}

function isoLocal(ms: number): string {
  const d = new Date(ms);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** Operator's request form for one FAIL row: reason (10–2000) and expiry (5 min – 30 days). */
function RequestException({ planId, gate, row, user, onDone }: { planId: string; gate: GateResult; row: ControlRow; user: DevToken | null; onDone: () => void }) {
  const api = useApi();
  const qc = useQueryClient();
  const [reason, setReason] = useState('');
  const [compensating, setCompensating] = useState('');
  const [expires, setExpires] = useState(() => isoLocal(Date.now() + 7 * 24 * 3600_000));
  const scope = `exception-request:${planId}:${row.control_id}:${gate.verdict_id}`;
  const send = useMutation({
    mutationFn: () =>
      api.requestException(
        user?.token ?? '',
        planId,
        { control_id: row.control_id, gate_verdict_id: gate.verdict_id, evidence_digest: row.evidence_digest ?? '', reason: reason.trim(), compensating_control: compensating.trim(), expires_at: new Date(expires).toISOString() },
        idemKey(scope),
      ),
    onSuccess: () => onDone(),
    onSettled: (_d, err) => {
      if (!(err instanceof ApiError && err.status === 0)) releaseIdemKey(scope);
      void qc.invalidateQueries({ queryKey: ['plan', planId] });
    },
  });
  const exp = Date.parse(expires);
  const problems: string[] = [];
  if (reason.trim().length < 10) problems.push('사유는 10자 이상 씁니다: 왜 목적지에서 대체할 수 없는지.');
  if (compensating.trim().length < 10) problems.push('대체 통제는 10자 이상 씁니다: 대신 둔 통제.');
  const evBlock = evidenceBlock(gate);
  if (evBlock) problems.push(evBlock);
  if (!(exp > Date.now() + 5 * 60_000 && exp < Date.now() + 30 * 24 * 3600_000)) problems.push('만료는 지금부터 5분 뒤에서 30일 뒤 사이여야 합니다.');
  return (
    <form
      className="exception__form"
      onSubmit={(e) => {
        e.preventDefault();
        if (problems.length === 0) send.mutate();
      }}
    >
      <p className="muted">
        {row.control_id} · 원본 {row.src} · 대상 {row.dst} · 판정 <span className="mono">{gate.verdict_id.slice(0, 8)}</span> · 증거{' '}
        <span className="mono" title={row.evidence_digest ?? undefined}>
          {(row.evidence_digest ?? '').replace(/^sha256:/, '').slice(0, 14)}…
        </span>
      </p>
      <label className="field">
        <span className="field__label">사유 (10–2000자)</span>
        <textarea className="input" rows={3} maxLength={2000} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="왜 목적지에서 이 통제를 걸 수 없는지…" />
      </label>
      <label className="field">
        <span className="field__label">대체 통제 (필수, 10–2000자)</span>
        <textarea className="input" rows={2} maxLength={2000} required value={compensating} onChange={(e) => setCompensating(e.target.value)} placeholder="대신 둔 통제…" />
      </label>
      <label className="field field--inline">
        <span className="field__label">만료</span>
        <input className="input" type="datetime-local" value={expires} min={isoLocal(Date.now() + 6 * 60_000)} max={isoLocal(Date.now() + 30 * 24 * 3600_000 - 60_000)} onChange={(e) => setExpires(e.target.value)} />
      </label>
      <Reasons id={`exreq-${row.control_id}`} reasons={problems} />
      <div className="exception__actions">
        <button type="submit" className="btn btn--primary" disabled={problems.length > 0 || send.isPending}>
          {send.isPending ? '요청 중…' : '예외 요청 보내기'}
        </button>
        <button type="button" className="btn btn--ghost" onClick={onDone}>
          취소
        </button>
      </div>
      <ErrorLine text={send.error ? errorText(send.error) : null} />
    </form>
  );
}

/** States in which the API accepts a gate run (service.py gate_run). */
const GATE_STATES = new Set(['COLLECT', 'GATE', 'AWAIT_APPROVAL']);

export function GatePanel({ planId, view, user }: { planId: string; view: ConsoleView; user: DevToken | null }) {
  const api = useApi();
  const qc = useQueryClient();
  const scope = `gate:${planId}`;
  const run = useMutation({
    mutationFn: () => {
      if (!user) throw new Error('사용자를 고르세요.');
      return api.runGate(user.token, planId, idemKey(scope));
    },
    onSettled: (_d, err) => {
      // Keep the key only when the request may not have reached the server.
      if (!(err instanceof ApiError && err.status === 0)) releaseIdemKey(scope);
      void qc.invalidateQueries({ queryKey: ['plan', planId] });
    },
  });

  const reasons: string[] = [];
  if (user?.role !== 'operator') reasons.push('게이트 실행은 operator 역할만 할 수 있습니다.');
  if (!GATE_STATES.has(view.state)) reasons.push(`상태가 COLLECT, GATE, AWAIT_APPROVAL일 때만 실행합니다. 지금은 ${view.state}입니다.`);
  if (view.approval) reasons.push('이미 승인을 받았습니다. 다시 돌리면 새 revision이 생겨 승인이 무효가 됩니다.');
  const disabled = reasons.length > 0 || run.isPending;

  // The POST response carries readiness values (lag_bytes, schema_match) that plan_get and
  // the event omit; show them while they belong to the verdict on screen.
  const gate =
    view.gate && run.data && run.data.verdict_id === view.gate.verdict_id ? { ...view.gate, readiness: run.data.readiness } : view.gate;
  const anyDerived = gate?.controls.some((c) => c.verdictDerived) ?? false;
  const exQ = useExceptions(planId, user?.token ?? '');
  const [requesting, setRequesting] = useState<string | null>(null);
  const openFor = (controlId: string): ControlException | undefined =>
    exQ.data?.find((e) => e.control_id === controlId && (e.status === 'requested' || e.status === 'approved'));
  const counts = { pass: 0, block: 0, exception: 0 };
  for (const c of gate?.controls ?? []) {
    if (c.verdict === 'PASS') counts.pass += 1;
    else if (c.verdict === 'EXCEPTION') counts.exception += 1;
    else counts.block += 1;
  }
  // Without a readable exceptions list the console cannot tell whether one is already open: no request button.
  const canRequest = user?.role === 'operator' && !EXCEPTION_LOCKED.has(view.state) && !exQ.error && exQ.data !== undefined;

  return (
    <Panel
      id="gate"
      step="판정"
      title="원본 대조 게이트"
      className="panel--gate"
      actions={
        <button
          type="button"
          className="btn btn--primary"
          onClick={() => run.mutate()}
          disabled={disabled}
          aria-describedby={reasons.length ? 'gate-reasons' : undefined}
        >
          {run.isPending ? '게이트 실행 중…' : '게이트 실행'}
        </button>
      }
    >
      <Reasons id="gate-reasons" reasons={reasons} />
      <ErrorLine text={run.error ? errorText(run.error) : null} />

      {gate ? (
        <>
          <div className={`verdict-head verdict-head--${gate.verdict === 'PASS' ? 'pass' : 'block'}`}>
            <Mark tone={gate.verdict === 'PASS' ? 'pass' : 'block'} size="lg">
              {gate.verdict}
            </Mark>
            <dl className="kv kv--inline">
              <div>
                <dt>판정 ID</dt>
                <dd className="mono">{gate.verdict_id}</dd>
              </div>
              <div>
                <dt>revision</dt>
                <dd className="mono">{gate.revision ?? '—'}</dd>
              </div>
              <div>
                <dt>판정 시각</dt>
                <dd className="mono">{fmtDateTime(gate.decided_at)}</dd>
              </div>
              <div>
                <dt>증거 유효</dt>
                <dd className="mono">{gate.evidence_expires_at ? `~${fmtTs(gate.evidence_expires_at)}${Date.parse(gate.evidence_expires_at) <= Date.now() ? ' (만료)' : ''}` : '— 판정에 없음'}</dd>
              </div>
              {view.gateRequestedBy && (
                <div>
                  <dt>요청</dt>
                  <dd>
                    <ActorBadge actor={view.gateRequestedBy} />
                  </dd>
                </div>
              )}
            </dl>
          </div>

          {gate.reasons.length > 0 && (
            <p className="reason-codes">
              <span className="muted">서버 사유 코드</span>{' '}
              {gate.reasons.map((r) => (
                <code key={r}>{r}</code>
              ))}
            </p>
          )}

          <p className="parity-summary" aria-label="동등 판정 요약">
            통제 {gate.controls.length}개 · 통과 <b>{counts.pass}</b> · 막음 <b>{counts.block}</b> · 예외 승인 <b>{counts.exception}</b>
            <span className="muted"> (예외 승인은 동등 판정에서 따로 빼서 셉니다)</span>
            {exQ.error ? <span className="muted"> · 예외 목록 연결 안 됨: {exceptionsError(exQ.error)} · 예외 요청 버튼을 숨깁니다</span> : null}
          </p>
          <div className="table-wrap" tabIndex={0} role="region" aria-label="통제 비교표">
            <table className="table">
              <caption className="sr-only">통제 ID별 원본, 대상, 판정</caption>
              <thead>
                <tr>
                  <th scope="col">통제 ID · 질문</th>
                  <th scope="col">
                    원본 <span className="th-sub">출발 환경</span>
                  </th>
                  <th scope="col">
                    대상 <span className="th-sub">목적 환경</span>
                  </th>
                  <th scope="col">판정</th>
                  <th scope="col">예외</th>
                </tr>
              </thead>
              <tbody>
                {gate.controls.map((c) => {
                  const applied = c.verdict === 'EXCEPTION' ? gate.exceptions.find((e) => e.exception_id === c.exception_id || e.control_id === c.control_id) : undefined;
                  const open = openFor(c.control_id);
                  return (
                  <Fragment key={c.control_id}>
                  <tr className={c.verdict === 'BLOCK' ? 'row--block' : undefined}>
                    <th scope="row">
                      <span className="mono">{c.control_id}</span>
                      {c.question && <span className="cell-sub">{c.question}</span>}
                    </th>
                    <td>
                      <ControlValueMark value={c.src} />
                    </td>
                    <td>
                      <ControlValueMark value={c.dst} />
                    </td>
                    <td>
                      {c.verdict === 'EXCEPTION' ? (
                        <>
                          <Mark tone="neutral">예외 승인</Mark>
                          <span className="cell-sub">
                            승인 {applied?.approved_by ?? '—'} · 기간 ~{fmtTs(applied?.expires_at ?? null)}
                          </span>
                          <span className="cell-sub">대체 통제: {applied?.compensating_control ?? '— 판정에 없음'}</span>
                        </>
                      ) : (
                        <Mark tone={c.verdict === 'PASS' ? 'pass' : 'block'}>
                          {c.verdict}
                          {c.verdictDerived && <sup title="API가 행 판정을 보내지 않아 화면에서 규칙으로 계산">*</sup>}
                        </Mark>
                      )}
                      {c.reason && c.verdict !== 'EXCEPTION' && <span className="cell-sub mono">{c.reason}</span>}
                    </td>
                    <td>
                      {c.verdict === 'EXCEPTION' ? (
                        <span className="muted">동등 판정에서 뺌</span>
                      ) : open ? (
                        <span className="muted">{EXCEPTION_STATUS[open.status]?.text ?? open.status} · {open.requested_by ?? '—'}</span>
                      ) : exceptable(c) ? (
                        evidenceBlock(gate) ? (
                          <span className="muted" title={evidenceBlock(gate) ?? undefined}>
                            {gate.evidence_expires_at ? `증거 만료(${fmtTs(gate.evidence_expires_at)})` : '증거 유효기간 없음'} · 게이트를 다시 돌린 뒤 요청
                          </span>
                        ) : canRequest ? (
                          <button type="button" className="btn btn--ghost" onClick={() => setRequesting(requesting === c.control_id ? null : c.control_id)} aria-expanded={requesting === c.control_id}>
                            예외 요청
                          </button>
                        ) : (
                          <span className="muted">{user?.role !== 'operator' ? '요청은 operator' : `${view.state}에서는 요청 불가`}</span>
                        )
                      ) : c.src === 'UNKNOWN' || c.dst === 'UNKNOWN' ? (
                        <span className="muted">UNKNOWN은 예외 불가</span>
                      ) : c.verdict === 'BLOCK' && c.dst === 'FAIL' && c.evidence_digest === null ? (
                        <span className="muted">증거 없음 · 예외 불가</span>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                  </tr>
                  {requesting === c.control_id && (
                    <tr className="row--form">
                      <td colSpan={5}>
                        <RequestException planId={planId} gate={gate} row={c} user={user} onDone={() => setRequesting(null)} />
                      </td>
                    </tr>
                  )}
                  </Fragment>
                  );
                })}
                {gate.controls.length === 0 && (
                  <tr>
                    <td colSpan={5} className="empty-cell">
                      판정 결과에 통제 행이 없습니다.
                    </td>
                  </tr>
                )}
              </tbody>
              <tbody className="tbody--readiness">
                <tr className="row--section">
                  <th scope="rowgroup" colSpan={5}>
                    데이터 경로 준비
                  </th>
                </tr>
                {gate.readiness.map((r) => (
                  <tr key={r.check_id} className={r.verdict === 'BLOCK' ? 'row--block' : undefined}>
                    <th scope="row">
                      {r.label}
                      {r.note && <span className="cell-sub">{r.note}</span>}
                    </th>
                    <td className="mono">{r.src ?? '—'}</td>
                    <td className="mono">{r.dst ?? '—'}</td>
                    <td>
                      {r.verdict ? (
                        <Mark tone={r.verdict === 'PASS' ? 'pass' : 'block'}>{r.verdict}</Mark>
                      ) : (
                        <Mark tone="unknown">판정 없음</Mark>
                      )}
                    </td>
                    <td />
                  </tr>
                ))}
                {gate.readiness.length === 0 && (
                  <tr>
                    <td colSpan={5} className="empty-cell">
                      준비 값(복제 지연, 스키마 해시)이 판정 결과에 없습니다.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <p className="footnote">
            규칙: 원본과 대상이 모두 PASS일 때만 통과합니다. FAIL이나 UNKNOWN이 하나라도 있으면 BLOCK입니다. 측정된 대상 FAIL은 approver가 승인한 예외로만 "예외 승인"이 되어 동등 판정에서 빠집니다. UNKNOWN은 예외로 바꿀 수 없습니다.
            {anyDerived && ' * 표시는 API가 행 판정을 보내지 않아 이 규칙으로 화면에서 계산한 값입니다.'}
          </p>
        </>
      ) : (
        <p className="empty">아직 게이트 판정이 없습니다. operator가 MCP <code>gate_run</code>이나 이 버튼으로 실행합니다.</p>
      )}
    </Panel>
  );
}
