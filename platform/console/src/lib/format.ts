const timeFmt = new Intl.DateTimeFormat('ko-KR', {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
});
const dateTimeFmt = new Intl.DateTimeFormat('ko-KR', {
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
});

function parse(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function fmtTime(iso: string | null | undefined): string {
  const d = parse(iso);
  if (!d) return '—';
  return `${timeFmt.format(d)}.${String(d.getMilliseconds()).padStart(3, '0')}`;
}

export function fmtDateTime(iso: string | null | undefined): string {
  const d = parse(iso);
  return d ? dateTimeFmt.format(d) : '—';
}

export function fmtMs(ms: number | null | undefined): string {
  return ms === null || ms === undefined ? '—' : ms.toLocaleString('ko-KR');
}

/** Remaining time until `iso`, or null when unknown. Negative means expired. */
export function remainingMs(iso: string | null | undefined, now: number): number | null {
  const d = parse(iso);
  return d ? d.getTime() - now : null;
}

export function fmtRemaining(ms: number): string {
  if (ms <= 0) return '만료됨';
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  return m > 0 ? `${m}분 ${s % 60}초 남음` : `${s}초 남음`;
}

export function shortId(id: string | null | undefined, n = 8): string {
  if (!id) return '—';
  return id.length > n + 1 ? `${id.slice(0, n)}…` : id;
}
