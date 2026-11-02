import type { StreamStatus } from '../api/client';
import type { PlanEvent } from '../api/types';
import { fmtTime } from '../lib/format';
import { ActorBadge, ModeBadge, Panel } from './bits';

const KIND_TEXT: Record<string, string> = {
  'state.changed': '상태 바뀜',
  'gate.verdict': '게이트 판정',
  'approval.granted': '승인됨',
  'run.stage': '실행 단계',
  'run.done': '실행 끝',
  'receipt.saved': '영수증 저장',
};

const STATUS_TEXT: Record<StreamStatus, string> = {
  connecting: '연결 중',
  open: '연결됨',
  reconnecting: '다시 연결 중',
  closed: '끊김',
  denied: '거부됨',
};

function summary(e: PlanEvent): string | null {
  const d = e.data;
  switch (e.kind) {
    case 'state.changed':
      return typeof d.from === 'string' ? `${d.from} → ${e.state}` : `→ ${e.state}`;
    case 'gate.verdict':
      return typeof d.verdict === 'string' ? `${d.verdict}${typeof d.verdict_id === 'string' ? ` · ${d.verdict_id}` : ''}` : null;
    case 'run.stage':
      return typeof d.seq === 'number' ? `${d.seq}. ${String(d.name ?? '')}${typeof d.ms === 'number' ? ` · ${d.ms} ms` : ''}` : null;
    case 'run.done':
      return `${String(d.status ?? '')}${typeof d.write_pause_ms === 'number' ? ` · 쓰기 정지 ${d.write_pause_ms} ms` : ''}`;
    case 'receipt.saved':
      return typeof d.verdict === 'string' ? d.verdict : null;
    case 'approval.granted':
      return typeof d.approver === 'string' ? d.approver : null;
    default:
      return null;
  }
}

export function EventStream({ events, status, detail }: { events: PlanEvent[]; status: StreamStatus; detail: string | null }) {
  const newest = [...events].reverse();
  const mcpCount = events.filter((e) => e.actor.kind === 'mcp').length;
  return (
    <Panel
      id="events"
      step="흐름"
      title="이벤트"
      className="panel--events"
      actions={
        <span className={`conn conn--${status}`} title={detail ?? undefined}>
          <span className="conn__dot" aria-hidden="true" />
          {STATUS_TEXT[status]}
          {detail && status !== 'open' ? <span className="sr-only">: {detail}</span> : null}
        </span>
      }
    >
      <p className="events__meta">
        {events.length}건 · MCP 요청 {mcpCount}건 · 새 이벤트가 위에 옵니다
      </p>
      {newest.length === 0 ? (
        <p className="empty">아직 받은 이벤트가 없습니다.</p>
      ) : (
        <ol className="events" role="log" aria-label="계획 이벤트, 최신순" aria-live="off">
          {newest.map((e) => {
            const s = summary(e);
            return (
              <li key={e.id} className={`event event--${e.actor.kind}`}>
                <time className="event__time mono" dateTime={e.at}>
                  {fmtTime(e.at)}
                </time>
                <span className="event__kind">
                  {KIND_TEXT[e.kind] ?? e.kind}
                  <span className="event__code mono">{e.kind}</span>
                </span>
                <span className="event__state mono">{e.state}</span>
                <span className="event__badges">
                  <ModeBadge mode={e.mode} />
                  <ActorBadge actor={e.actor} />
                </span>
                {s && <span className="event__summary mono">{s}</span>}
              </li>
            );
          })}
        </ol>
      )}
    </Panel>
  );
}
