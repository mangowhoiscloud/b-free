import { useEffect, useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { errorText } from '../api/client';
import type { ConsoleView } from '../api/derive';
import { asNum } from '../api/normalize';
import type { DevToken } from '../api/types';
import { fmtDateTime, fmtRemaining, remainingMs } from '../lib/format';
import { idemKey } from '../lib/idem';
import { useApi, useNow } from '../state';
import { ConfirmDialog, ErrorLine, Mark, Panel, Reasons } from './bits';

export function ApprovalPanel({ planId, view, user }: { planId: string; view: ConsoleView; user: DevToken | null }) {
  const api = useApi();
  const qc = useQueryClient();
  const now = useNow();
  const [confirming, setConfirming] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const req = view.awaiting;
  const approval = view.approval;
  const granted =
    approval !== null && (req === null || approval.approval_request_id === null || approval.approval_request_id === req.approval_request_id);

  // The approve URL from MCP plan_get ends with #approve: bring this panel into view.
  useEffect(() => {
    if (location.hash === '#approve') wrapRef.current?.querySelector<HTMLElement>('section')?.focus();
  }, []);

  // Values the approval is bound to. Before the decision they come from the request,
  // after it from the approval record.
  const revision = req?.revision ?? (granted ? approval?.revision : null) ?? null;
  const gateVerdictId = req?.gate_verdict_id ?? (granted ? approval?.gate_verdict_id : null) ?? null;
  const expiresAt = granted ? (approval?.expires_at ?? null) : null;
  const revisionNum = asNum(req?.revision);

  const approve = useMutation({
    mutationFn: () => {
      if (!user || !req || revisionNum === null) throw new Error('승인 요청이 없습니다.');
      // One key per one-time approval request: a retry can never decide twice.
      return api.approve(
        user.token,
        planId,
        {
          stage: 'cutover',
          approval_request_id: req.approval_request_id,
          revision: revisionNum,
          gate_verdict_id: req.gate_verdict_id,
        },
        idemKey(`approve:${req.approval_request_id}`),
      );
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: ['plan', planId] }),
  });

  const left = remainingMs(expiresAt, now);

  const reasons: string[] = [];
  if (user?.role !== 'approver') reasons.push('승인은 approver 역할만 할 수 있습니다.');
  if (view.state !== 'AWAIT_APPROVAL') reasons.push(`상태가 AWAIT_APPROVAL일 때만 승인합니다. 지금은 ${view.state}입니다.`);
  else if (granted) reasons.push('이 승인 요청은 이미 승인되었습니다.');
  else if (!req) reasons.push('승인 요청(approval_request_id)을 아직 받지 못했습니다.');
  else if (revisionNum === null) reasons.push('승인 요청의 revision이 숫자가 아닙니다.');
  if (user && view.createdBy && user.email === view.createdBy) reasons.push('계획을 만든 사람은 승인할 수 없습니다(직무 분리).');
  if (user && view.approvalRequestedBy && user.email === view.approvalRequestedBy && view.approvalRequestedBy !== view.createdBy) {
    reasons.push('승인을 요청한 사람(통과한 게이트를 돌린 사람)은 승인할 수 없습니다(직무 분리).');
  }
  const disabled = reasons.length > 0 || approve.isPending;

  return (
    <div ref={wrapRef} className="col-item">
      <Panel id="approve" step="승인" title="결속된 승인" className="panel--approval">
        <p className="rule">
          <strong>승인은 콘솔에서만 합니다.</strong> MCP에는 승인 도구가 없습니다. 승인자는 계획을 만든 사람, 승인을 요청한 사람과 달라야 합니다.
        </p>

        <dl className="kv kv--grid">
          <div>
            <dt>계획 revision</dt>
            <dd className="mono">{revision ?? '—'}</dd>
          </div>
          <div>
            <dt>게이트 판정 ID</dt>
            <dd className="mono">{gateVerdictId ?? '—'}</dd>
          </div>
          <div>
            <dt>만료</dt>
            <dd className="mono">
              {expiresAt ? (
                <>
                  {fmtDateTime(expiresAt)}
                  {left !== null && <span className={`countdown${left <= 0 ? ' countdown--over' : ''}`}> · {fmtRemaining(left)}</span>}
                </>
              ) : req ? (
                <span className="muted">승인하는 때부터 셈</span>
              ) : (
                '—'
              )}
            </dd>
          </div>
          <div>
            <dt>승인 요청 ID</dt>
            <dd className="mono">{req?.approval_request_id ?? (granted ? approval?.approval_request_id : null) ?? '—'}</dd>
          </div>
          <div>
            <dt>계획 작성자</dt>
            <dd>{view.createdBy ?? '—'}</dd>
          </div>
          <div>
            <dt>승인 요청자</dt>
            <dd>{view.approvalRequestedBy ?? '—'}</dd>
          </div>
        </dl>

        {granted && approval && (
          <div className="granted">
            <Mark tone="pass">승인됨</Mark>
            <span>
              {approval.approver ?? '승인자 정보 없음'} · {fmtDateTime(approval.granted_at)} · 승인 ID{' '}
              <span className="mono">{approval.approval_id}</span>
              {approval.consumed && ' · 실행에 쓰임'}
            </span>
          </div>
        )}

        <div className="action-row">
          <button
            type="button"
            className="btn btn--approve"
            disabled={disabled}
            onClick={() => setConfirming(true)}
            aria-describedby={reasons.length ? 'approval-reasons' : undefined}
          >
            {approve.isPending ? '승인 보내는 중…' : '승인'}
          </button>
          <Reasons id="approval-reasons" reasons={reasons} />
        </div>
        <ErrorLine text={approve.error ? errorText(approve.error) : null} />

        <ConfirmDialog
          open={confirming}
          title="이 값으로 컷오버를 승인합니다"
          confirmText="승인"
          busy={approve.isPending}
          onCancel={() => setConfirming(false)}
          onConfirm={() => {
            setConfirming(false);
            approve.mutate();
          }}
        >
          <dl className="kv">
            <div>
              <dt>계획</dt>
              <dd className="mono">{planId}</dd>
            </div>
            <div>
              <dt>revision</dt>
              <dd className="mono">{req?.revision}</dd>
            </div>
            <div>
              <dt>게이트 판정 ID</dt>
              <dd className="mono">{req?.gate_verdict_id ?? '—'}</dd>
            </div>
            <div>
              <dt>단계</dt>
              <dd className="mono">{req?.stage ?? 'cutover'}</dd>
            </div>
            <div>
              <dt>승인자</dt>
              <dd>{user?.email}</dd>
            </div>
          </dl>
          <p className="muted">승인은 이 revision에만 묶입니다. 계획이 바뀌면 승인은 무효가 됩니다.</p>
        </ConfirmDialog>
      </Panel>
    </div>
  );
}
