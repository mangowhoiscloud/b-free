import type { ReactNode } from 'react';
import { Card } from '@astryxdesign/core/Card';
import { MetadataList, MetadataListItem } from '@astryxdesign/core/MetadataList';
import { Heading, Text } from '@astryxdesign/core/Text';
import { HStack, VStack } from '@astryxdesign/core/Stack';
import { Table, pixel, proportional, type TableColumn } from '@astryxdesign/core/Table';

/** A screen answers one question (DESIGN-REFERENCE §3): title, the question in one line, then the tools. */
export function Screen({ title, lead, aside, children }: { title: string; lead: ReactNode; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className="screen" aria-labelledby="screen-title">
      <header className="screen__head">
        <div>
          <Heading level={2} id="screen-title">
            {title}
          </Heading>
          <p className="screen__q">{lead}</p>
        </div>
        {aside && <div className="screen__aside">{aside}</div>}
      </header>
      <VStack gap={4}>{children}</VStack>
    </section>
  );
}

/** One surface per section. Nothing inside it draws another box. */
export function Block({ title, meta, children, id }: { title: ReactNode; meta?: ReactNode; children: ReactNode; id?: string }) {
  return (
    <Card padding={4} className="block">
      <VStack gap={3}>
        <HStack gap={2} hAlign="between" vAlign="center" wrap="wrap">
          <Heading level={3} id={id}>
            {title}
          </Heading>
          {meta && <HStack gap={3} vAlign="center" wrap="wrap">{meta}</HStack>}
        </HStack>
        {children}
      </VStack>
    </Card>
  );
}

export interface Col<T> {
  key: string;
  header: string;
  render: (row: T) => ReactNode;
  weight?: number;
  /** Numbers: right-aligned with tabular figures, fixed narrow width unless `px` is given. */
  numeric?: boolean;
  px?: number;
}

/** Astryx Table over plain rows. Rows are wrapped so any shape works. */
export function DataTable<T>({ rows, cols, rowKey, empty }: { rows: T[]; cols: Col<T>[]; rowKey: (r: T, i: number) => string; empty: string }) {
  if (rows.length === 0)
    return (
      <Text type="supporting" color="secondary" as="p">
        {empty}
      </Text>
    );
  type Row = { _k: string; _r: T } & Record<string, unknown>;
  const data: Row[] = rows.map((r, i) => ({ _k: rowKey(r, i), _r: r }));
  const columns: TableColumn<Row>[] = cols.map((c) => ({
    key: c.key,
    header: c.header,
    width: c.px ? pixel(c.px) : c.numeric ? pixel(104) : proportional(c.weight ?? 1),
    align: c.numeric ? 'end' : 'start',
    renderCell: (row: Row) => (c.numeric ? <span className="num">{c.render(row._r)}</span> : c.render(row._r)),
  }));
  return <Table data={data} columns={columns} idKey="_k" density="compact" textOverflow="wrap" />;
}

/** Description list (Astryx MetadataList): label / value pairs instead of stat tiles. */
export function Facts({ items, columns = 'multi' }: { items: { label: string; value: ReactNode }[]; columns?: 'multi' | 'single' | number }) {
  return (
    <MetadataList columns={columns}>
      {items.map((i) => (
        <MetadataListItem key={i.label} label={i.label}>
          {i.value}
        </MetadataListItem>
      ))}
    </MetadataList>
  );
}

/** A value with its unit, or "—" plus the reason when there is no value. */
export function Val({ v, unit, why }: { v: string | number | null | undefined; unit?: string; why?: string }) {
  if (v === null || v === undefined || v === '')
    return (
      <span className="num">
        —{why && <span className="why"> {why}</span>}
      </span>
    );
  return (
    <span className="num">
      {v}
      {unit && <span className="unit">{unit}</span>}
    </span>
  );
}

export interface Kpi {
  label: string;
  value: ReactNode;
  note?: ReactNode;
}

/** KPI row: one surface, the only 24px figures on a screen. */
export function KpiRow({ items, label }: { items: Kpi[]; label: string }) {
  return (
    <div className="kpis" role="group" aria-label={label}>
      {items.map((k) => (
        <div key={k.label} className="kpi">
          <span className="kpi__label">{k.label}</span>
          <span className="kpi__value">{k.value}</span>
          {k.note && <span className="kpi__note">{k.note}</span>}
        </div>
      ))}
    </div>
  );
}

export type Health = 'ok' | 'warn' | 'bad' | 'none' | 'run';
export const HEALTH_TEXT: Record<Health, string> = { ok: '정상', warn: '준비 중', bad: '응답 없음', none: '연결 안 됨', run: '진행 중' };

/** Status dot + word: color never alone. */
export function StatusText({ h, text }: { h: Health; text?: string }) {
  return (
    <span className="nowrap">
      <span className={`dot dot--${h}`} aria-hidden="true" />
      {text ?? HEALTH_TEXT[h]}
    </span>
  );
}

/** Ratio bar of health counts (Argo CD), with the counts in text. */
export function StatusBar({ counts, label }: { counts: Partial<Record<Health, number>>; label: string }) {
  const order: Health[] = ['ok', 'run', 'warn', 'bad', 'none'];
  const total = order.reduce((a, h) => a + (counts[h] ?? 0), 0);
  return (
    <div className="statusbar" role="group" aria-label={label}>
      <div className="statusbar__track" aria-hidden="true">
        {total > 0 &&
          order.map((h) =>
            counts[h] ? <span key={h} className={`statusbar__seg--${h === 'run' ? 'ok' : h}`} style={{ flexGrow: counts[h] }} /> : null,
          )}
      </div>
      <div className="statusbar__legend">
        {order.filter((h) => counts[h]).map((h) => (
          <StatusText key={h} h={h} text={`${HEALTH_TEXT[h]} ${counts[h]}`} />
        ))}
        {total === 0 && <span>표시할 대상 없음</span>}
      </div>
    </div>
  );
}
