import { useMutation, useQueryClient } from '@tanstack/react-query';
import { ApiError, errorText } from '../api/client';
import type { ConsoleView } from '../api/derive';
import type { DevToken } from '../api/types';
import { fmtDateTime } from '../lib/format';
import { idemKey, releaseIdemKey } from '../lib/idem';
import { useApi } from '../state';
import { ActorBadge, ControlValueMark, ErrorLine, Mark, Panel, Reasons } from './bits';

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

          <div className="table-wrap" tabIndex={0} role="region" aria-label="통제 비교표">
            <table className="table">
              <caption className="sr-only">통제 ID별 원본, 대상, 판정</caption>
              <thead>
                <tr>
                  <th scope="col">통제 ID · 질문</th>
                  <th scope="col">
                    원본 <span className="th-sub">AWS</span>
                  </th>
                  <th scope="col">
                    대상 <span className="th-sub">온프렘</span>
                  </th>
                  <th scope="col">판정</th>
                </tr>
              </thead>
              <tbody>
                {gate.controls.map((c) => (
                  <tr key={c.control_id} className={c.verdict === 'BLOCK' ? 'row--block' : undefined}>
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
                      <Mark tone={c.verdict === 'PASS' ? 'pass' : 'block'}>
                        {c.verdict}
                        {c.verdictDerived && <sup title="API가 행 판정을 보내지 않아 화면에서 규칙으로 계산">*</sup>}
                      </Mark>
                      {c.reason && <span className="cell-sub mono">{c.reason}</span>}
                    </td>
                  </tr>
                ))}
                {gate.controls.length === 0 && (
                  <tr>
                    <td colSpan={4} className="empty-cell">
                      판정 결과에 통제 행이 없습니다.
                    </td>
                  </tr>
                )}
              </tbody>
              <tbody className="tbody--readiness">
                <tr className="row--section">
                  <th scope="rowgroup" colSpan={4}>
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
                  </tr>
                ))}
                {gate.readiness.length === 0 && (
                  <tr>
                    <td colSpan={4} className="empty-cell">
                      준비 값(복제 지연, 스키마 해시)이 판정 결과에 없습니다.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <p className="footnote">
            규칙: 원본과 대상이 모두 PASS일 때만 통과합니다. FAIL이나 UNKNOWN이 하나라도 있으면 BLOCK입니다.
            {anyDerived && ' * 표시는 API가 행 판정을 보내지 않아 이 규칙으로 화면에서 계산한 값입니다.'}
          </p>
        </>
      ) : (
        <p className="empty">아직 게이트 판정이 없습니다. operator가 MCP <code>gate_run</code>이나 이 버튼으로 실행합니다.</p>
      )}
    </Panel>
  );
}
