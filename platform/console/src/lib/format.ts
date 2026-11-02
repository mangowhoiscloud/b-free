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

/* DESIGN-REFERENCE §5 number and time formats. */
const tsFmt = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
const clockFmt = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Seoul', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });

/** `2026-11-02 21:31 UTC+9` (KST and JST share the offset). */
export function fmtTs(v: string | number | null | undefined): string {
  if (v === null || v === undefined || v === '') return '—';
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? '—' : `${tsFmt.format(d)} UTC+9`;
}

/** `21:31:08` in UTC+9, for "last updated" lines next to a dated context. */
export function fmtClock(v: number | null | undefined): string {
  if (!v) return '—';
  return clockFmt.format(new Date(v));
}

/** Latency: integer ms under 1000, else seconds with two decimals. */
export function fmtLatency(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return '—';
  return ms < 1000 ? `${Math.round(ms).toLocaleString('en-US')} ms` : `${(ms / 1000).toFixed(2)} s`;
}

/** Ratio as percent with one decimal: `0.4 %`. */
export function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  return `${v.toFixed(1)} %`;
}

/** Money `$1,234.56`; hourly unit prices keep four decimals (`$0.0832`). */
export function fmtMoney(v: number | null | undefined, currency = 'USD', unitPrice = false): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  const digits = unitPrice ? 4 : 2;
  try {
    return new Intl.NumberFormat('en-US', { style: 'currency', currency, minimumFractionDigits: digits, maximumFractionDigits: digits }).format(v);
  } catch {
    return `${v.toFixed(digits)} ${currency}`;
  }
}

/** Counts with thousands separators. */
export function fmtCount(v: number | null | undefined): string {
  return v === null || v === undefined || !Number.isFinite(v) ? '—' : Math.round(v).toLocaleString('en-US');
}
