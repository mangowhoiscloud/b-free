/**
 * Control exceptions (S1 5b, contract docs/architecture/15-r2-contracts.md §9).
 * The approver decides here, next to the evidence of the FAIL being excepted. Approver ≠ requester ≠ plan
 * author (the API and a DB trigger enforce it; the console explains it before the click). UNKNOWN can never
 * be excepted. Approving does not change the current verdict: the next gate run applies it.
 */
import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError, errorText } from '../api/client';
import type { ConsoleView } from '../api/derive';
import type { ControlException, DevToken } from '../api/types';
import { fmtTs, shortId } from '../lib/format';
import { idemKey, releaseIdemKey } from '../lib/idem';
import { getJson, useConfig } from '../live/live';
import { useApi, useNow } from '../state';
import { ErrorLine, Mark, Panel, Reasons, type Tone } from './bits';

/** A 404 without a reason code means the route itself is missing, not the plan. */
export function exceptionsError(err: unknown): string {
  if (err instanceof ApiError && err.status === 404 && !err.code) return 'API가 예외 경로를 모릅니다(404)';
  return errorText(err);
}

export const EXCEPTION_STATUS: Record<string, { text: string; tone: Tone }> = {
  requested: { text: '승인 대기', tone: 'unknown' },
  approved: { text: '예외 승인', tone: 'neutral' },
  rejected: { text: '거절됨', tone: 'block' },
  stale: { text: '무효(증거 바뀜)', tone: 'block' },
  expired: { text: '만료', tone: 'unknown' },
};

/** Exceptions of a plan; refreshed on plan events (same query-key prefix as the plan). */
export function useExceptions(planId: string, token: string) {
  const api = useApi();
  return useQuery({ queryKey: ['plan', planId, 'exceptions'], queryFn: () => api.listExceptions(token, planId), refetchInterval: 15_000, retry: 0 });
}

function short(d: string | null, n = 14) {
  return d ? shortId(d.replace(/^sha256:/, ''), n) : '—';
}

function MeasuringNote({ since }: { since: number | null }) {
  const [start] = useState(() => since ?? Date.now());
  const now = useNow(1000);
  return (
    <p className="muted" role="status">
      게이트가 지금 측정하는 중… {Math.round((now - start) / 1000)}초 (보통 16–60초, 90초가 지나면 연결 안 됨으로 표시)
    </p>
  );
}

/** Current measurement from the gate service (GET /v1/controls/report, read-only), on demand. */
function CurrentEvidence({ controlId, source, target, expected }: { controlId: string; source: string | null; target: string | null; expected: string | null }) {
  const { svcBase } = useConfig();
  const [on, setOn] = useState(false);
  const path = source && target ? `v1/controls/report?source=${encodeURIComponent(source)}&target=${encodeURIComponent(target)}` : null;
  const q = useQuery({
    queryKey: ['gate-report', source, target],
    enabled: on && Boolean(path),
    retry: 0,
    staleTime: 60_000,
    queryFn: async () => {
      const o = (await getJson(`${svcBase}/gate/${path}`, 90_000)) as { questions?: Record<string, unknown>[] };
      return (o.questions ?? []).find((r) => r.control_id === controlId) ?? null;
    },
  });
  if (!path) return <p className="muted">현재 측정: 연결 안 됨 · 출발 · 목적 환경 이름을 알 수 없어 게이트 보고서를 부르지 않습니다.</p>;
  if (!on)
    return (
      <button type="button" className="btn btn--ghost" onClick={() => setOn(true)}>
        현재 측정 증거 보기 (게이트 보고서)
      </button>
    );
  if (q.isLoading) return <MeasuringNote since={q.dataUpdatedAt || null} />;
  if (q.error) return <p className="muted">현재 측정: 연결 안 됨 · GET /svc/gate/{path} · {errorText(q.error)}</p>;
  const row = q.data as Record<string, unknown> | null;
  if (!row) return <p className="muted">현재 측정: 게이트 보고서에 {controlId} 행이 없습니다.</p>;
  const digest = typeof row.dst_evidence_digest === 'string' ? row.dst_evidence_digest : null;
  const same = digest !== null && expected !== null && digest === expected;
  return (
    <div className="evidence">
      <p>
        현재 목적지 값 <code>{String(row.dst ?? '—')}</code> · 증거 digest <code>{short(digest)}</code>{' '}
        {expected && <Mark tone={same ? 'pass' : 'block'}>{same ? '예외의 증거와 같음' : '예외의 증거와 다름'}</Mark>}
      </p>
      <pre className="evidence__json">{JSON.stringify(row.dst_evidence ?? null, null, 2).slice(0, 2000)}</pre>
    </div>
  );
}

function Decide({ planId, ex, view, user, names }: { planId: string; ex: ControlException; view: ConsoleView; user: DevToken | null; names: { source: string | null; target: string | null } }) {
  const api = useApi();
  const qc = useQueryClient();
  const [note, setNote] = useState('');
  const scope = `exception:${ex.exception_id}`;
  const settle = (err: unknown) => {
    if (!(err instanceof ApiError && err.status === 0)) releaseIdemKey(scope);
    void qc.invalidateQueries({ queryKey: ['plan', planId] });
  };
  const approve = useMutation({
    mutationFn: () => api.approveException(user?.token ?? '', planId, ex.exception_id, { evidence_digest: ex.evidence_digest ?? '', ...(note.trim() ? { note: note.trim() } : {}) }, idemKey(`${scope}:approve`)),
    onSettled: (_d, err) => settle(err),
  });
  const reject = useMutation({
    mutationFn: () => api.rejectException(user?.token ?? '', planId, ex.exception_id, note.trim() ? { note: note.trim() } : {}, idemKey(`${scope}:reject`)),
    onSettled: (_d, err) => settle(err),
  });
  const row = view.gate?.controls.find((c) => c.control_id === ex.control_id) ?? null;
  const rowSame = row !== null && row.src === ex.src && row.dst === ex.dst && row.evidence_digest === ex.evidence_digest;
  const reasons: string[] = [];
  if (user?.role !== 'approver') reasons.push('승인 · 거절은 approver 역할만 할 수 있습니다.');
  if (user && ex.requested_by && user.email === ex.requested_by) reasons.push('예외를 요청한 사람은 승인 · 거절할 수 없습니다(직무 분리).');
  if (user && view.createdBy && user.email === view.createdBy) reasons.push('계획을 만든 사람은 승인 · 거절할 수 없습니다(직무 분리).');
  const approveReasons = [...reasons];
  if (!row) approveReasons.push('마지막 게이트 판정에 이 통제 행이 없어 승인할 수 없습니다.');
  else if (!rowSame) approveReasons.push('마지막 게이트 판정의 이 행이 예외를 요청할 때와 달라(값 또는 증거) 승인할 수 없습니다. 새로 요청해야 합니다.');
  if (ex.expires_at && Date.parse(ex.expires_at) <= Date.now()) approveReasons.push('예외의 만료 시각이 지났습니다.');
  const evExp = view.gate?.evidence_expires_at ?? null;
  if (view.gate && !evExp) approveReasons.push('마지막 판정에 증거 유효기간이 없어(게이트 보고서를 읽지 못한 판정) 승인할 수 없습니다. operator가 게이트를 다시 돌려야 합니다.');
  else if (evExp && Date.parse(evExp) <= Date.now()) approveReasons.push(`마지막 판정의 증거가 만료되었습니다(${fmtTs(evExp)}). 만료된 증거로는 승인하지 않습니다. operator가 게이트를 다시 돌려야 합니다.`);
  const busy = approve.isPending || reject.isPending;
  return (
    <div className="exception__decide">
      <h4 className="exception__h">예외로 뺄 FAIL과 그 증거</h4>
      <dl className="kv kv--inline">
        <div>
          <dt>통제</dt>
          <dd className="mono">{ex.control_id}</dd>
        </div>
        <div>
          <dt>원본 · 대상</dt>
          <dd className="mono">
            {ex.src ?? '—'} · {ex.dst ?? '—'}
          </dd>
        </div>
        <div>
          <dt>요청 때 판정</dt>
          <dd className="mono">{shortId(ex.gate_verdict_id)}</dd>
        </div>
        <div>
          <dt>증거 digest</dt>
          <dd className="mono" title={ex.evidence_digest ?? undefined}>
            {short(ex.evidence_digest)}
          </dd>
        </div>
        <div>
          <dt>증거 유효</dt>
          <dd className="mono">{evExp ? `~${fmtTs(evExp)}` : '— 판정에 없음'}</dd>
        </div>
      </dl>
      <p>
        <b>대체 통제</b> {ex.compensating_control ?? <span className="muted">— 응답에 없음</span>}
      </p>
      <p>
        마지막 판정의 이 행:{' '}
        {row ? (
          <>
            <code>
              {row.src} · {row.dst}
            </code>{' '}
            증거 <code>{short(row.evidence_digest)}</code> <Mark tone={rowSame ? 'pass' : 'block'}>{rowSame ? '요청과 같음' : '요청과 다름'}</Mark>
            {row.reason && <span className="muted"> 사유 {row.reason}</span>}
          </>
        ) : (
          <span className="muted">없음(게이트를 다시 돌려야 함)</span>
        )}
      </p>
      <CurrentEvidence controlId={ex.control_id} source={names.source} target={names.target} expected={ex.evidence_digest} />
      <label className="field">
        <span className="field__label">결정 메모(선택, 2000자 이하)</span>
        <textarea className="input" rows={2} maxLength={2000} value={note} onChange={(e) => setNote(e.target.value)} />
      </label>
      <Reasons id={`ex-reasons-${ex.exception_id}`} reasons={approveReasons} />
      <div className="exception__actions">
        <button type="button" className="btn btn--approve" disabled={approveReasons.length > 0 || busy} onClick={() => approve.mutate()}>
          {approve.isPending ? '승인 중…' : '예외 승인'}
        </button>
        <button type="button" className="btn btn--danger" disabled={reasons.length > 0 || busy} onClick={() => reject.mutate()}>
          {reject.isPending ? '거절 중…' : '거절'}
        </button>
      </div>
      <ErrorLine text={approve.error ? errorText(approve.error) : reject.error ? errorText(reject.error) : null} />
    </div>
  );
}

export function ExceptionsPanel({ planId, view, user, names }: { planId: string; view: ConsoleView; user: DevToken | null; names: { source: string | null; target: string | null } }) {
  const q = useExceptions(planId, user?.token ?? '');
  const list = q.data ?? [];
  const pending = list.filter((e) => e.status === 'requested');
  return (
    <Panel id="exceptions" step="예외" title={`통제 예외 · 승인 대기 ${pending.length}건`} className="panel--exceptions">
      {q.error ? (
        <p className="empty" role="status">
          ○ 연결 안 됨 · GET /v1/plans/{shortId(planId)}/exceptions · {exceptionsError(q.error)}
        </p>
      ) : q.isLoading ? (
        <p className="empty">예외 목록을 불러오는 중…</p>
      ) : list.length === 0 ? (
        <p className="empty">이 계획에는 통제 예외가 없습니다. operator가 게이트 표의 FAIL 행에서 요청합니다(UNKNOWN은 예외 불가).</p>
      ) : (
        <ul className="exceptions">
          {list.map((ex) => {
            const st = EXCEPTION_STATUS[ex.status] ?? { text: ex.status, tone: 'unknown' as Tone };
            return (
              <li key={ex.exception_id} className="exception">
                <div className="exception__head">
                  <span className="mono">{ex.control_id}</span>
                  <Mark tone={st.tone}>{st.text}</Mark>
                  {ex.applied && <span className="muted">마지막 판정에 반영됨</span>}
                  {ex.stale_reason && <span className="muted">({ex.stale_reason})</span>}
                </div>
                <p className="exception__reason">
                  <b>사유</b> {ex.reason ?? '—'}
                </p>
                <p className="exception__reason">
                  <b>대체 통제</b> {ex.compensating_control ?? <span className="muted">— 응답에 없음</span>}
                </p>
                <p className="muted">
                  {ex.target_environment ?? '목적지 —'} · 기간 ~{fmtTs(ex.expires_at)} · 요청 {ex.requested_by ?? '—'} {fmtTs(ex.requested_at)}
                  {ex.decided_by ? ` · 결정 ${ex.decided_by} ${fmtTs(ex.decided_at)}` : ''}
                  {ex.decision_note ? ` · 메모 ${ex.decision_note}` : ''}
                </p>
                {ex.status === 'requested' && <Decide planId={planId} ex={ex} view={view} user={user} names={names} />}
              </li>
            );
          })}
        </ul>
      )}
      <p className="footnote">승인해도 지금 판정은 바뀌지 않습니다. operator가 게이트를 다시 돌리면 게이트가 증거를 다시 재고, 같을 때만 그 행이 "예외 승인"이 됩니다.</p>
    </Panel>
  );
}
