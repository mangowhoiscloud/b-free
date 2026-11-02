/**
 * 기록 재생 (contract docs/architecture/15-r2-contracts.md §12): a finished run replayed from
 * GET /v1/plans/{id}/runs/{run_id}/replay. Every event is `mode: recorded` with its original time and
 * `offset_ms`. A persistent banner names the original time and run_id so it is never taken for live.
 * Replay writes nothing.
 */
import { useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Button } from '@astryxdesign/core/Button';
import { Selector } from '@astryxdesign/core/Selector';
import { HStack } from '@astryxdesign/core/Stack';
import { errorText } from '../api/client';
import type { ConsoleView } from '../api/derive';
import type { ShiftSummary } from '../api/types';
import { fmtTs, shortId } from '../lib/format';
import { useApi, type Session } from '../state';
import { useHashParam } from './hashQuery';
import { Block, DataTable, Screen, Val } from './ScreenFrame';
import { NotConnected } from './live';

const KIND_TEXT: Record<string, string> = { shift: '전환(10/50/100)', cutover: 'DB 컷오버', failback: '되돌리기' };

function stageText(data: Record<string, unknown>): string {
  const st = (data.stage && typeof data.stage === 'object' ? data.stage : data) as Record<string, unknown>;
  const name = typeof st.name === 'string' ? st.name : '';
  const res = typeof st.result === 'string' ? st.result : '';
  const ms = typeof st.ms === 'number' ? ` · ${st.ms} ms` : '';
  return [name, res].filter(Boolean).join(' · ') + ms;
}

export function ReplayScreen({ planId, session, view, shiftInfo }: { planId: string | null; session: Session; view: ConsoleView | null; shiftInfo: ShiftSummary | null }) {
  const api = useApi();
  const token = session.user?.token ?? '';
  const [runParam, setRun] = useHashParam('run', '');
  const candidates = useMemo(() => {
    const out: { value: string; label: string }[] = [];
    if (shiftInfo) out.push({ value: shiftInfo.run_id, label: `전환 ${shortId(shiftInfo.run_id)} · ${shiftInfo.status}` });
    if (view?.run && !out.some((o) => o.value === view.run?.run_id)) out.push({ value: view.run.run_id, label: `${KIND_TEXT[view.run.kind ?? ''] ?? view.run.kind ?? '실행'} ${shortId(view.run.run_id)} · ${view.run.status}` });
    if (runParam && !out.some((o) => o.value === runParam)) out.push({ value: runParam, label: `실행 ${shortId(runParam)}` });
    return out;
  }, [shiftInfo, view, runParam]);
  const runId = runParam || candidates[0]?.value || '';
  const q = useQuery({ queryKey: ['replay', planId, runId, token], enabled: Boolean(planId && runId), queryFn: () => api.getReplay(token, planId ?? '', runId), retry: 0, staleTime: Infinity });
  const events = q.data?.events ?? [];
  const [shown, setShown] = useState<number>(Number.POSITIVE_INFINITY);
  const [speed, setSpeed] = useState(10);
  const [playing, setPlaying] = useState(false);

  // Step through events at their recorded offsets (scaled by speed).
  useEffect(() => {
    if (!playing) return;
    const next = shown === Number.POSITIVE_INFINITY ? 0 : shown;
    if (next >= events.length) {
      setPlaying(false);
      return;
    }
    const cur = events[next - 1]?.offset_ms ?? 0;
    const due = events[next]?.offset_ms ?? cur;
    const t = setTimeout(() => setShown(next + 1), Math.max(0, (due - cur) / speed));
    return () => clearTimeout(t);
  }, [playing, shown, events, speed]);

  const visible = shown === Number.POSITIVE_INFINITY ? events : events.slice(0, shown);
  const r = q.data;

  return (
    <Screen title="기록 재생" lead="끝난 실행을 원래 시각과 간격 그대로 다시 봅니다. 실시간이 아닙니다.">
      {!planId ? (
        <NotConnected title="계획 없음" endpoint="GET /v1/plans/{id}/runs/{run_id}/replay" reason="재생할 실행은 계획에 붙어 있습니다. 홈의 계획 표에서 계획을 고르세요." />
      ) : candidates.length === 0 ? (
        <NotConnected title="재생할 실행 없음" endpoint="GET /v1/plans/{id}" reason="이 계획에는 끝난 실행이 없습니다." />
      ) : (
        <>
          {r && (
            <div className="replay-banner" role="status" aria-live="polite">
              <b>기록 재생 · 원래 시각 {fmtTs(r.recorded_at)}</b>
              <span className="mono">run {r.run_id}</span>
              <span>
                {KIND_TEXT[r.kind ?? ''] ?? r.kind ?? '—'} · {r.status ?? '—'} · 끝 {fmtTs(r.ended_at)}
              </span>
              <span>실시간 아님 · mode {r.mode}{r.mode !== 'recorded' ? ' (recorded가 아님: 응답 확인 필요)' : ''}</span>
            </div>
          )}
          <HStack gap={2} vAlign="end" wrap="wrap">
            <Selector label="실행" options={candidates} value={runId} onChange={(v) => { setRun(v); setShown(Number.POSITIVE_INFINITY); setPlaying(false); }} width={320} size="sm" />
            <Selector label="속도" options={[{ value: '1', label: '원래 속도' }, { value: '10', label: '10배' }, { value: '60', label: '60배' }]} value={String(speed)} onChange={(v) => setSpeed(Number(v))} width={120} size="sm" />
            <Button label={playing ? '재생 중…' : '처음부터 재생'} size="sm" variant="secondary" isDisabled={!r || events.length === 0 || playing} clickAction={() => { setShown(0); setPlaying(true); }} />
            <Button label="전부 보기" size="sm" variant="secondary" isDisabled={!r} clickAction={() => { setPlaying(false); setShown(Number.POSITIVE_INFINITY); }} />
          </HStack>
          <Block title={`기록된 이벤트 ${visible.length} / ${events.length}`}>
            {q.error ? (
              <NotConnected endpoint={`GET /v1/plans/${shortId(planId)}/runs/${shortId(runId)}/replay`} reason={errorText(q.error)} />
            ) : q.isLoading ? (
              <p className="meta">기록을 불러오는 중…</p>
            ) : (
              <DataTable
                rows={visible}
                rowKey={(e) => e.id}
                empty="아직 재생하지 않았습니다. 처음부터 재생을 누르세요."
                cols={[
                  { key: 'o', header: '경과', numeric: true, render: (e) => <Val v={e.offset_ms === null ? null : (e.offset_ms / 1000).toFixed(1)} unit="s" /> },
                  { key: 't', header: '원래 시각', px: 190, render: (e) => <span className="meta">{fmtTs(e.at)}</span> },
                  { key: 'k', header: '종류', render: (e) => <span className="mono">{e.kind}</span> },
                  { key: 'd', header: '내용', weight: 4, render: (e) => <span className="mono">{e.kind === 'run.stage' ? stageText(e.data) : e.kind === 'run.done' ? `${String(e.data.kind ?? '')} · ${String(e.data.status ?? '')}` : e.kind === 'receipt.saved' ? `영수증 ${String(e.data.verdict ?? '')}` : ''}</span> },
                  { key: 'm', header: '모드', render: (e) => <span className="mono">{e.mode}</span> },
                ]}
              />
            )}
          </Block>
        </>
      )}
    </Screen>
  );
}
