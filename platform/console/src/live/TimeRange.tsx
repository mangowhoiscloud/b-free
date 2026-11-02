/**
 * The one time-range picker per screen (DESIGN-REFERENCE §5): presets 15m/1h/6h/24h and "컷오버 ±30분"
 * (around the open plan's run), a refresh interval (off/5s/15s) and the last refresh time. Kept in the URL.
 */
import { Selector } from '@astryxdesign/core/Selector';
import { HStack } from '@astryxdesign/core/Stack';
import { fmtClock } from '../lib/format';
import { useHashParam } from './hashQuery';

export interface TimeWindow {
  key: string;
  label: string;
  /** epoch ms */
  from: number;
  to: number;
  refreshMs: number | false;
  /** true when the cutover preset was chosen but no cutover time is known. */
  missingCutover: boolean;
}

const PRESETS: Record<string, { label: string; minutes: number }> = {
  '15m': { label: '최근 15분', minutes: 15 },
  '1h': { label: '최근 1시간', minutes: 60 },
  '6h': { label: '최근 6시간', minutes: 360 },
  '24h': { label: '최근 24시간', minutes: 1440 },
};

/** `cutover`: [start, end] of the open plan's run in epoch ms, or null. */
export function useTimeWindow(cutover: [number, number] | null): [TimeWindow, { range: string; setRange: (v: string) => void; refresh: string; setRefresh: (v: string) => void }] {
  const [range, setRange] = useHashParam('range', '1h');
  const [refresh, setRefresh] = useHashParam('refresh', '15');
  const refreshMs = refresh === 'off' ? false : Number(refresh) * 1000;
  // Round "now" to the refresh step so query keys stay stable between renders.
  const step = refreshMs || 60_000;
  const now = Math.floor(Date.now() / step) * step;
  if (range === 'cutover') {
    const w: TimeWindow = cutover
      ? { key: 'cutover', label: '컷오버 ±30분', from: cutover[0] - 30 * 60_000, to: cutover[1] + 30 * 60_000, refreshMs, missingCutover: false }
      : { key: 'cutover', label: '컷오버 ±30분', from: now - 60 * 60_000, to: now, refreshMs, missingCutover: true };
    return [w, { range, setRange, refresh, setRefresh }];
  }
  const p = PRESETS[range] ?? PRESETS['1h']!;
  return [{ key: range, label: p.label, from: now - p.minutes * 60_000, to: now, refreshMs, missingCutover: false }, { range, setRange, refresh, setRefresh }];
}

export function TimeRangePicker({ ctl, updatedAt }: { ctl: { range: string; setRange: (v: string) => void; refresh: string; setRefresh: (v: string) => void }; updatedAt: number }) {
  return (
    <HStack gap={2} vAlign="center" wrap="wrap">
      <Selector
        label="시간 범위"
        isLabelHidden
        size="sm"
        width={150}
        value={ctl.range}
        onChange={ctl.setRange}
        options={[...Object.entries(PRESETS).map(([value, p]) => ({ value, label: p.label })), { value: 'cutover', label: '컷오버 ±30분' }]}
      />
      <Selector
        label="새로 고침"
        isLabelHidden
        size="sm"
        width={110}
        value={ctl.refresh}
        onChange={ctl.setRefresh}
        options={[
          { value: 'off', label: '새로 고침 끔' },
          { value: '5', label: '5초마다' },
          { value: '15', label: '15초마다' },
        ]}
      />
      <span className="meta">갱신 {fmtClock(updatedAt)} UTC+9</span>
    </HStack>
  );
}
