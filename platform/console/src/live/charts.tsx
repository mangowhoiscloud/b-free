/**
 * One charting approach for the console: hand-built SVG (no chart library), following the dataviz skill.
 * - one y axis per chart (never dual axes); two measures -> two charts (small multiples)
 * - categorical series colors in fixed order (--series-1..3, validated light & dark), text in ink tokens
 * - 2px lines, recessive solid gridlines, crosshair + one tooltip listing every series, keyboard arrows too
 * - legend with line keys for >= 2 series; a table view under every chart
 */
import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from 'react';

export interface Series {
  id: string;
  label: string;
  /** Fixed color slot for this entity (environment); keeps color when filters drop other series. */
  colorIndex?: number;
  /** [epoch ms, value] sorted by time. */
  points: [number, number][];
}

export interface Band {
  from: number;
  to: number;
  label: string;
}

/** A plan event drawn as a vertical line on the shared time axis. */
export interface Marker {
  t: number;
  label: string;
}

const SERIES_VARS = ['var(--series-1)', 'var(--series-2)', 'var(--series-3)', 'var(--series-4)'];
const M = { top: 12, right: 16, bottom: 26, left: 52 };

function useWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T | null>(null);
  const [w, setW] = useState(600);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver((entries) => {
      const cw = entries[0]?.contentRect.width;
      if (cw && Math.abs(cw - w) > 1) setW(cw);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [w]);
  return [ref, w];
}

function niceMax(v: number): number {
  if (!(v > 0)) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

const hhmmFmt = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Seoul', hour: '2-digit', minute: '2-digit', hour12: false });
const mdhmFmt = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Seoul', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
const hhmm = (t: number) => hhmmFmt.format(new Date(t));

export function fmtValue(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  return v.toLocaleString('ko-KR', { maximumFractionDigits: digits });
}

/** Line chart over time. Colors follow the series' position in `series` (fixed order, never re-ranked). */
export function LineChart({ title, series, unit, digits = 2, height = 200, bands = [], markers = [], domain, format, empty }: { title: string; series: Series[]; unit: string; digits?: number; height?: number; bands?: Band[]; markers?: Marker[]; domain?: [number, number]; format?: (v: number) => string; empty: string }) {
  const [wrapRef, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const times = useMemo(() => [...new Set(series.flatMap((s) => s.points.map((p) => p[0])))].sort((a, b) => a - b), [series]);
  const hasData = times.length > 0;
  const t0 = domain?.[0] ?? times[0] ?? 0;
  const t1 = domain?.[1] ?? times[times.length - 1] ?? 1;
  const fmt = format ?? ((v: number) => `${fmtValue(v, digits)} ${unit}`);
  const yMax = niceMax(Math.max(0, ...series.flatMap((s) => s.points.map((p) => p[1])).filter(Number.isFinite)));
  const iw = Math.max(10, width - M.left - M.right);
  const ih = height - M.top - M.bottom;
  const x = (t: number) => M.left + (t1 === t0 ? iw / 2 : ((t - t0) / (t1 - t0)) * iw);
  const y = (v: number) => M.top + ih - (v / yMax) * ih;
  const yTicks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * yMax);
  const xTicks = hasData ? [0, 0.25, 0.5, 0.75, 1].map((f) => t0 + f * (t1 - t0)) : [];
  const spanMin = (t1 - t0) / 60_000;
  const hoverT = hover !== null ? times[hover] : undefined;

  const onMove = (e: PointerEvent<SVGSVGElement>) => {
    if (!hasData) return;
    const r = e.currentTarget.getBoundingClientRect();
    const px = e.clientX - r.left;
    let best = 0;
    let bestD = Infinity;
    times.forEach((t, i) => {
      const d = Math.abs(x(t) - px);
      if (d < bestD) {
        bestD = d;
        best = i;
      }
    });
    setHover(best);
  };
  const onKey = (e: KeyboardEvent<SVGSVGElement>) => {
    if (!hasData) return;
    if (e.key === 'ArrowLeft') setHover((h) => Math.max(0, (h ?? times.length) - 1));
    else if (e.key === 'ArrowRight') setHover((h) => Math.min(times.length - 1, (h ?? -1) + 1));
    else if (e.key === 'Escape') setHover(null);
    else return;
    e.preventDefault();
  };

  return (
    <figure className="chart">
      <figcaption className="chart__head">
        <span className="chart__title">{title}</span>
        <span className="chart__unit">단위: {unit}</span>
        {series.length >= 2 && (
          <span className="chart__legend">
            {series.map((s, i) => (
              <span key={s.id} className="chart__key">
                <svg width="14" height="8" aria-hidden="true">
                  <line x1="0" y1="4" x2="14" y2="4" stroke={SERIES_VARS[s.colorIndex ?? i] ?? 'var(--ink-3)'} strokeWidth="2" strokeLinecap="round" />
                </svg>
                {s.label}
              </span>
            ))}
          </span>
        )}
      </figcaption>
      <div className="chart__plot" ref={wrapRef}>
        {!hasData ? (
          <p className="foot chart__empty">{empty}</p>
        ) : (
          <>
            <svg
              width={width}
              height={height}
              role="img"
              aria-label={`${title}, ${hhmm(t0)}–${hhmm(t1)}, 화살표 키로 시점 이동`}
              tabIndex={0}
              onPointerMove={onMove}
              onPointerLeave={() => setHover(null)}
              onKeyDown={onKey}
              onBlur={() => setHover(null)}
            >
              {bands.map((b) => (
                <g key={`${b.from}-${b.label}`}>
                  <rect x={x(Math.max(t0, b.from))} y={M.top} width={Math.max(2, x(Math.min(t1, b.to)) - x(Math.max(t0, b.from)))} height={ih} className="chart__band" />
                  <text x={x(Math.max(t0, b.from)) + 4} y={M.top + 12} className="chart__bandlabel">
                    {b.label}
                  </text>
                </g>
              ))}
              {markers
                .filter((m) => m.t >= t0 && m.t <= t1)
                .map((m, i) => (
                  <g key={`${m.t}-${i}`}>
                    <line x1={x(m.t)} x2={x(m.t)} y1={M.top} y2={M.top + ih} className="chart__marker" />
                    <text x={x(m.t) + 3} y={M.top + 12 + (i % 3) * 12} className="chart__bandlabel">
                      {m.label}
                    </text>
                  </g>
                ))}
              {yTicks.map((v) => (
                <g key={v}>
                  <line x1={M.left} x2={M.left + iw} y1={y(v)} y2={y(v)} className="chart__grid" />
                  <text x={M.left - 6} y={y(v) + 4} textAnchor="end" className="chart__tick">
                    {fmtValue(v, v < 10 ? 2 : 0)}
                  </text>
                </g>
              ))}
              {xTicks.map((t, i) => (
                <text key={t} x={x(t)} y={height - 6} textAnchor={i === 0 ? 'start' : i === xTicks.length - 1 ? 'end' : 'middle'} className="chart__tick">
                  {spanMin > 1440 ? mdhmFmt.format(new Date(t)) : hhmm(t)}
                </text>
              ))}
              {series.map((s, i) => (
                <path
                  key={s.id}
                  d={s.points.filter((p) => Number.isFinite(p[1])).map((p, j) => `${j === 0 ? 'M' : 'L'}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(' ')}
                  fill="none"
                  stroke={SERIES_VARS[s.colorIndex ?? i] ?? 'var(--ink-3)'}
                  strokeWidth={2}
                  strokeLinejoin="round"
                  strokeLinecap="round"
                />
              ))}
              {hoverT !== undefined && (
                <g>
                  <line x1={x(hoverT)} x2={x(hoverT)} y1={M.top} y2={M.top + ih} className="chart__cross" />
                  {series.map((s, i) => {
                    const p = s.points.find((q) => q[0] === hoverT);
                    return p && Number.isFinite(p[1]) ? <circle key={s.id} cx={x(p[0])} cy={y(p[1])} r={4} fill={SERIES_VARS[s.colorIndex ?? i] ?? 'var(--ink-3)'} className="chart__dot" /> : null;
                  })}
                </g>
              )}
            </svg>
            {hoverT !== undefined && (
              <div className="chart__tip" style={{ left: Math.min(Math.max(x(hoverT) + 12, 0), width - 200) }} role="status">
                <div className="chart__tiptime">{hhmm(hoverT)}</div>
                {series.map((s, i) => {
                  const p = s.points.find((q) => q[0] === hoverT);
                  return (
                    <div key={s.id} className="chart__tiprow">
                      <svg width="12" height="8" aria-hidden="true">
                        <line x1="0" y1="4" x2="12" y2="4" stroke={SERIES_VARS[s.colorIndex ?? i] ?? 'var(--ink-3)'} strokeWidth="2" strokeLinecap="round" />
                      </svg>
                      <b className="num">{p && Number.isFinite(p[1]) ? fmt(p[1]) : '—'}</b>
                      <span>{s.label}</span>
                    </div>
                  );
                })}
              </div>
            )}
          </>
        )}
      </div>
      {hasData && (
        <details className="chart__table">
          <summary>표로 보기</summary>
          <table className="plain">
            <thead>
              <tr>
                <th scope="col">시계열</th>
                <th scope="col" className="td-end">마지막 ({unit})</th>
                <th scope="col" className="td-end">최소</th>
                <th scope="col" className="td-end">최대</th>
              </tr>
            </thead>
            <tbody>
              {series.map((s) => {
                const vals = s.points.map((p) => p[1]).filter(Number.isFinite);
                return (
                  <tr key={s.id}>
                    <td>{s.label}</td>
                    <td className="td-end num">{fmtValue(vals[vals.length - 1], digits)}</td>
                    <td className="td-end num">{fmtValue(vals.length ? Math.min(...vals) : null, digits)}</td>
                    <td className="td-end num">{fmtValue(vals.length ? Math.max(...vals) : null, digits)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </details>
      )}
    </figure>
  );
}

export interface Span {
  id: string;
  label: string;
  /** ms from the first span's start. */
  start: number;
  ms: number;
}

/** Horizontal timeline (Gantt) of stages. One hue: the stages are one category. Bars are the hit targets. */
export function Timeline({ title, spans, highlight }: { title: string; spans: Span[]; highlight?: { from: number; to: number; label: string } }) {
  const [wrapRef, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<string | null>(null);
  const total = Math.max(1, ...spans.map((s) => s.start + s.ms), highlight ? highlight.to : 0);
  const left = 150;
  const iw = Math.max(10, width - left - 16);
  const row = 22;
  const h = spans.length * row + 30;
  const x = (ms: number) => left + (ms / total) * iw;
  const hov = spans.find((s) => s.id === hover);
  return (
    <figure className="chart">
      <figcaption className="chart__head">
        <span className="chart__title">{title}</span>
        <span className="chart__unit">단위: ms(첫 단계 시작 기준)</span>
      </figcaption>
      <div className="chart__plot" ref={wrapRef}>
        <svg width={width} height={h} role="img" aria-label={`${title}, 단계 ${spans.length}개, 전체 ${fmtValue(total, 0)} ms`}>
          {highlight && (
            <g>
              <rect x={x(highlight.from)} y={4} width={Math.max(2, x(highlight.to) - x(highlight.from))} height={spans.length * row} className="chart__band" />
              <text x={x(highlight.from) + 4} y={h - 16} className="chart__bandlabel">
                {highlight.label}
              </text>
            </g>
          )}
          {spans.map((s, i) => (
            <g
              key={s.id}
              tabIndex={0}
              onPointerEnter={() => setHover(s.id)}
              onPointerLeave={() => setHover(null)}
              onFocus={() => setHover(s.id)}
              onBlur={() => setHover(null)}
              aria-label={`${s.label} ${fmtValue(s.ms, 0)} ms`}
            >
              <rect x={0} y={4 + i * row} width={width} height={row} fill="transparent" />
              <text x={left - 8} y={4 + i * row + 15} textAnchor="end" className="chart__tick">
                {s.label}
              </text>
              <rect x={x(s.start)} y={4 + i * row + 5} width={Math.max(3, x(s.start + s.ms) - x(s.start))} height={row - 10} rx={2} fill="var(--series-1)" className={hover === s.id ? 'chart__bar chart__bar--on' : 'chart__bar'} />
            </g>
          ))}
          <text x={left} y={h - 4} className="chart__tick">
            0
          </text>
          <text x={left + iw} y={h - 4} textAnchor="end" className="chart__tick">
            {fmtValue(total, 0)} ms
          </text>
        </svg>
        {hov && (
          <div className="chart__tip" style={{ left: Math.min(x(hov.start + hov.ms) + 8, width - 200) }} role="status">
            <div className="chart__tiprow">
              <b className="num">{fmtValue(hov.ms, 0)} ms</b>
              <span>{hov.label}</span>
            </div>
            <div className="chart__tiptime">시작 +{fmtValue(hov.start, 0)} ms</div>
          </div>
        )}
      </div>
    </figure>
  );
}

export interface StackDay {
  day: string;
  /** component -> amount */
  parts: Record<string, number>;
}

/** Daily stacked bars (OpenCost). Components keep a fixed color order; more than 4 fold into "기타". */
export function StackedBars({ title, days, unit, format }: { title: string; days: StackDay[]; unit: string; format: (v: number) => string }) {
  const [wrapRef, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<string | null>(null);
  const totals = new Map<string, number>();
  for (const d of days) for (const [k, v] of Object.entries(d.parts)) totals.set(k, (totals.get(k) ?? 0) + v);
  const ranked = [...totals.entries()].sort((a, b) => b[1] - a[1]).map(([k]) => k);
  const keep = ranked.slice(0, ranked.length > 4 ? 3 : 4).sort();
  const keys = ranked.length > 4 ? [...keep, '기타'] : keep;
  const fold = (parts: Record<string, number>) => {
    const out: Record<string, number> = {};
    for (const [k, v] of Object.entries(parts)) {
      const key = keep.includes(k) ? k : '기타';
      out[key] = (out[key] ?? 0) + v;
    }
    return out;
  };
  const folded = days.map((d) => ({ day: d.day, parts: fold(d.parts) }));
  const yMax = niceMax(Math.max(0, ...folded.map((d) => Object.values(d.parts).reduce((a, b) => a + b, 0))));
  const height = 200;
  const ih = height - M.top - M.bottom;
  const iw = Math.max(10, width - M.left - M.right);
  const bw = folded.length ? Math.max(6, Math.min(48, iw / folded.length - 8)) : 0;
  const y = (v: number) => (v / yMax) * ih;
  const hov = hover ? hover.split('|') : null;
  return (
    <figure className="chart">
      <figcaption className="chart__head">
        <span className="chart__title">{title}</span>
        <span className="chart__unit">단위: {unit}</span>
        <span className="chart__legend">
          {keys.map((k, i) => (
            <span key={k} className="chart__key">
              <svg width="10" height="10" aria-hidden="true">
                <rect width="10" height="10" rx="2" fill={SERIES_VARS[i] ?? 'var(--ink-3)'} />
              </svg>
              {k}
            </span>
          ))}
        </span>
      </figcaption>
      <div className="chart__plot" ref={wrapRef}>
        <svg width={width} height={height} role="img" aria-label={`${title}, ${folded.length}일`}>
          {[0, 0.5, 1].map((f) => (
            <g key={f}>
              <line x1={M.left} x2={M.left + iw} y1={M.top + ih - f * ih} y2={M.top + ih - f * ih} className="chart__grid" />
              <text x={M.left - 6} y={M.top + ih - f * ih + 4} textAnchor="end" className="chart__tick">
                {format(f * yMax)}
              </text>
            </g>
          ))}
          {folded.map((d, i) => {
            const cx = M.left + (iw / folded.length) * (i + 0.5);
            let acc = 0;
            return (
              <g key={d.day}>
                {keys.map((k, ki) => {
                  const v = d.parts[k] ?? 0;
                  if (v <= 0) return null;
                  const h = Math.max(1, y(v) - 2);
                  const top = M.top + ih - y(acc + v);
                  acc += v;
                  return (
                    <rect
                      key={k}
                      x={cx - bw / 2}
                      y={top}
                      width={bw}
                      height={h}
                      rx={2}
                      fill={SERIES_VARS[ki] ?? 'var(--ink-3)'}
                      tabIndex={0}
                      className={hover === `${d.day}|${k}` ? 'chart__bar chart__bar--on' : 'chart__bar'}
                      onPointerEnter={() => setHover(`${d.day}|${k}`)}
                      onPointerLeave={() => setHover(null)}
                      onFocus={() => setHover(`${d.day}|${k}`)}
                      onBlur={() => setHover(null)}
                      aria-label={`${d.day} ${k} ${format(v)}`}
                    />
                  );
                })}
                <text x={cx} y={height - 6} textAnchor="middle" className="chart__tick">
                  {d.day.slice(5)}
                </text>
              </g>
            );
          })}
        </svg>
        {hov && (
          <div className="chart__tip" role="status">
            <div className="chart__tiptime">{hov[0]}</div>
            <div className="chart__tiprow">
              <b className="num">{format(folded.find((d) => d.day === hov[0])?.parts[hov[1] ?? ''] ?? 0)}</b>
              <span>{hov[1]}</span>
            </div>
          </div>
        )}
      </div>
      <details className="chart__table">
        <summary>표로 보기</summary>
        <table className="plain">
          <thead>
            <tr>
              <th scope="col">날짜</th>
              {keys.map((k) => (
                <th key={k} scope="col" className="td-end">
                  {k}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {folded.map((d) => (
              <tr key={d.day}>
                <td className="num">{d.day}</td>
                {keys.map((k) => (
                  <td key={k} className="td-end num">
                    {d.parts[k] !== undefined ? format(d.parts[k]!) : '—'}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </figure>
  );
}
