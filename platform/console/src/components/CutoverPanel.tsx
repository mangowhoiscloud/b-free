import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { errorText } from '../api/client';
import type { ConsoleView } from '../api/derive';
import type { DevToken, HashPair, Receipt } from '../api/types';
import { fmtMs, remainingMs, shortId } from '../lib/format';
import { idemKey } from '../lib/idem';
import { useApi, useNow } from '../state';
import { ConfirmDialog, ErrorLine, Mark, Panel, Reasons, type Tone } from './bits';

/** Labels for Executor stage names (CONTRACT §6). Unknown names are shown as sent. */
const STAGE_LABEL: Record<string, string> = {
  lag_check_epoch: '지연 확인 · epoch 올림',
  pgbouncer_pause: 'PgBouncer PAUSE',
  src_nologin_terminate: '출발지 app_writer NOLOGIN · 세션 종료',
  barrier_final_compare: 'barrier · 최종 비교 · 시퀀스 올림',
  switch_subscriptions: '정방향 구독 끔 · 역방향 켬(6a)',
  commit_record: '확정 기록(6b)',
  dst_login_resume: '목적지 LOGIN · 대상 변경 · RESUME',
  verify_write: 'PgBouncer로 쓰기 확인',
};

function resultTone(result: string): Tone {
  const r = result.toUpperCase();
  if (['OK', 'DONE', 'PASS', 'SUCCESS', 'CONFIRMED_APPLIED'].includes(r)) return 'pass';
  if (['FAIL', 'FAILED', 'ERROR', 'ABORT', 'ABORT_PRE_WRITE', 'SAFE_HOLD'].includes(r)) return 'block';
  return 'unknown';
}

const RUN_STATUS_TEXT: Record<string, string> = {
  RUNNING: '진행 중',
  DONE: '완료',
  ABORT_PRE_WRITE: '쓰기 전 중단(원복)',
  SAFE_HOLD: '안전 정지',
};

export function CutoverPanel({ planId, view, user }: { planId: string; view: ConsoleView; user: DevToken | null }) {
  const api = useApi();
  const qc = useQueryClient();
  const now = useNow();
  const [confirming, setConfirming] = useState(false);
  const approval = view.approval;
  const req = view.awaiting;

  const shift = useMutation({
    mutationFn: () => {
      if (!user || !approval) throw new Error('승인이 없습니다.');
      // One key per approval: a retry or double click returns the same run_id (A8).
      return api.shift(user.token, planId, idemKey(`shift:${approval.approval_id}`));
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: ['plan', planId] }),
  });

  const approvalMatches = approval !== null && (!req || approval.approval_request_id === null || approval.approval_request_id === req.approval_request_id);
  const left = remainingMs(approval?.expires_at, now);
  const reasons: string[] = [];
  if (user?.role !== 'operator') reasons.push('컷오버 시작은 operator 역할만 할 수 있습니다.');
  if (view.state !== 'AWAIT_APPROVAL') reasons.push(`상태가 AWAIT_APPROVAL일 때만 시작합니다. 지금은 ${view.state}입니다.`);
  else if (!approvalMatches) reasons.push('현재 승인 요청에 대한 승인이 아직 없습니다.');
  if (left !== null && left <= 0) reasons.push('승인이 만료되었습니다.');
  if (approval?.consumed) reasons.push('이 승인은 이미 실행에 쓰였습니다. 새 실행에는 새 승인이 필요합니다.');
  const disabled = reasons.length > 0 || shift.isPending;

  const run = view.run;
  const budget = run?.write_pause_budget_ms ?? null;
  const pause = run?.write_pause_ms ?? null;
  const ratio = pause !== null && budget ? Math.min(pause / budget, 1) : null;

  return (
    <Panel
      id="cutover"
      step="실행"
      title="컷오버"
      className="panel--cutover"
      actions={
        <button
          type="button"
          className="btn btn--danger"
          disabled={disabled}
          onClick={() => setConfirming(true)}
          aria-describedby={reasons.length ? 'cutover-reasons' : undefined}
        >
          {shift.isPending ? '시작 요청 중…' : '컷오버 시작'}
        </button>
      }
    >
      <Reasons id="cutover-reasons" reasons={reasons} />
      <ErrorLine
        text={
          shift.error
            ? errorText(shift.error)
            : shift.data && shift.data.run_id === null
              ? `실행이 만들어지지 않았습니다.${shift.data.reason ? ` 사유 ${shift.data.reason}` : ''}`
              : null
        }
      />

      <div className="pause">
        <div className="pause__label">
          쓰기 정지
          <span className="pause__sub">PAUSE부터 RESUME까지</span>
        </div>
        <div className="pause__value" aria-live="polite">
          <span className="pause__num">{pause === null ? '—' : fmtMs(pause)}</span>
          <span className="pause__unit">ms</span>
        </div>
        {ratio !== null && budget !== null && (
          <div className="pause__budget">
            <div className="meter" role="img" aria-label={`예산 ${fmtMs(budget)} ms 중 ${Math.round(ratio * 100)}%`}>
              <span className="meter__fill" style={{ width: `${ratio * 100}%` }} />
            </div>
            <span className="muted">예산 {fmtMs(budget)} ms</span>
          </div>
        )}
      </div>

      {run ? (
        <>
          <dl className="kv kv--inline">
            <div>
              <dt>run_id</dt>
              <dd className="mono" title={run.run_id}>
                {shortId(run.run_id, 14)}
              </dd>
            </div>
            <div>
              <dt>상태</dt>
              <dd>
                <Mark tone={run.status === 'DONE' ? 'pass' : run.status === 'RUNNING' ? 'unknown' : 'block'}>
                  {RUN_STATUS_TEXT[run.status] ?? run.status}
                </Mark>
              </dd>
            </div>
            <div>
              <dt>단계</dt>
              <dd className="mono">{run.stages.length} / 8</dd>
            </div>
          </dl>
          <div className="table-wrap" tabIndex={0} role="region" aria-label="컷오버 단계">
            <table className="table table--stages">
              <caption className="sr-only">컷오버 단계별 결과와 걸린 시간</caption>
              <thead>
                <tr>
                  <th scope="col">#</th>
                  <th scope="col">단계</th>
                  <th scope="col">결과</th>
                  <th scope="col" className="num">
                    ms
                  </th>
                </tr>
              </thead>
              <tbody>
                {run.stages.map((s) => (
                  <tr key={s.seq}>
                    <td className="mono">{s.seq}</td>
                    <th scope="row">
                      {STAGE_LABEL[s.name] ?? s.name}
                      {STAGE_LABEL[s.name] && <span className="stage-code mono">{s.name}</span>}
                    </th>
                    <td>
                      <Mark tone={resultTone(s.result)}>{s.result || '—'}</Mark>
                    </td>
                    <td className="num mono">{fmtMs(s.ms)}</td>
                  </tr>
                ))}
                {run.stages.length === 0 && (
                  <tr>
                    <td colSpan={4} className="empty-cell">
                      단계 결과를 기다리는 중입니다.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <ReceiptView receipt={run.receipt} />
        </>
      ) : (
        <p className="empty">아직 실행이 없습니다. 승인 뒤 operator가 이 버튼으로 시작합니다. MCP로는 시작할 수 없습니다.</p>
      )}

      <ConfirmDialog
        open={confirming}
        title="컷오버를 시작합니다"
        confirmText="컷오버 시작"
        busy={shift.isPending}
        onCancel={() => setConfirming(false)}
        onConfirm={() => {
          setConfirming(false);
          shift.mutate();
        }}
      >
        <p>출발지 쓰기를 잠시 멈추고 목적지로 옮깁니다. 6a 단계 전 실패는 원복하고, 뒤의 실패는 안전 정지로 멈춥니다.</p>
        <dl className="kv">
          <div>
            <dt>승인 ID</dt>
            <dd className="mono">{approval?.approval_id}</dd>
          </div>
          <div>
            <dt>revision</dt>
            <dd className="mono">{approval?.revision ?? req?.revision ?? '—'}</dd>
          </div>
          <div>
            <dt>시작하는 사람</dt>
            <dd>{user?.email}</dd>
          </div>
        </dl>
      </ConfirmDialog>
    </Panel>
  );
}

function PairRow({ label, pair, expectEqual, note }: { label: string; pair: HashPair; expectEqual: boolean; note?: string }) {
  const both = pair.src !== null && pair.dst !== null;
  const same = both && pair.src === pair.dst;
  return (
    <tr className={expectEqual && both && !same ? 'row--block' : undefined}>
      <th scope="row">{label}</th>
      <td className="mono">{pair.src ?? '—'}</td>
      <td className="mono">{pair.dst ?? '—'}</td>
      <td>
        {!both ? (
          <Mark tone="unknown">값 없음</Mark>
        ) : expectEqual ? (
          <Mark tone={same ? 'pass' : 'block'}>{same ? '같음' : '다름'}</Mark>
        ) : (
          <span className="muted">{note}</span>
        )}
      </td>
    </tr>
  );
}

function ReceiptView({ receipt }: { receipt: Receipt | null }) {
  if (!receipt) {
    return <p className="empty">무결성 영수증은 5단계 최종 비교 뒤에 저장됩니다.</p>;
  }
  const match = receipt.verdict === 'MATCH';
  const noBody = [receipt.rows, receipt.pk_hash, receipt.content_hash, receipt.seq].every((p) => p.src === null && p.dst === null);
  return (
    <div className="receipt">
      <div className="receipt__head">
        <h3 className="receipt__title">무결성 영수증</h3>
        <Mark tone={match ? 'pass' : 'block'} size="lg">
          {receipt.verdict}
        </Mark>
        {receipt.receipt_id && <span className="mono muted">{receipt.receipt_id}</span>}
      </div>
      {noBody && <p className="footnote">API가 영수증 본문(행 수, 해시, 시퀀스)을 아직 보내지 않아 판정만 보입니다.</p>}
      <div className="table-wrap" tabIndex={0} role="region" aria-label="무결성 영수증">
        <table className="table">
          <caption className="sr-only">쓰기를 멈춘 뒤 출발지와 목적지의 최종 비교</caption>
          <thead>
            <tr>
              <th scope="col">항목</th>
              <th scope="col">
                출발지 <span className="th-sub">AWS</span>
              </th>
              <th scope="col">
                목적지 <span className="th-sub">온프렘</span>
              </th>
              <th scope="col">비교</th>
            </tr>
          </thead>
          <tbody>
            <PairRow label="행 수" pair={receipt.rows} expectEqual />
            <PairRow label="PK 집합 해시" pair={receipt.pk_hash} expectEqual />
            <PairRow label="내용 해시" pair={receipt.content_hash} expectEqual />
            <PairRow label="시퀀스" pair={receipt.seq} expectEqual={false} note="목적지는 5단계에서 올려 둠" />
          </tbody>
        </table>
      </div>
    </div>
  );
}
