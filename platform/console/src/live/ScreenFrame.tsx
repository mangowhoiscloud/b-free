import type { ReactNode } from 'react';
import { Card } from '@astryxdesign/core/Card';
import { Heading, Text } from '@astryxdesign/core/Text';
import { HStack, VStack } from '@astryxdesign/core/Stack';
import { Table, proportional, type TableColumn } from '@astryxdesign/core/Table';

export function Screen({ title, lead, aside, children }: { title: string; lead: ReactNode; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className="screen" aria-labelledby="screen-title">
      <header className="screen__head">
        <div>
          <Heading level={2} id="screen-title">
            {title}
          </Heading>
          <Text type="supporting" color="secondary" as="p">
            {lead}
          </Text>
        </div>
        {aside && <div className="screen__aside">{aside}</div>}
      </header>
      <VStack gap={4}>{children}</VStack>
    </section>
  );
}

export function Block({ title, meta, children, id }: { title: ReactNode; meta?: ReactNode; children: ReactNode; id?: string }) {
  return (
    <Card padding={4} className="block">
      <VStack gap={3}>
        <HStack gap={2} hAlign="between" vAlign="center" wrap="wrap">
          <Heading level={3} id={id}>
            {title}
          </Heading>
          {meta && <HStack gap={2} vAlign="center" wrap="wrap">{meta}</HStack>}
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
    width: proportional(c.weight ?? 1),
    renderCell: (row: Row) => c.render(row._r),
  }));
  return <Table data={data} columns={columns} idKey="_k" density="compact" textOverflow="wrap" />;
}

export function Stat({ label, value, unit, note }: { label: string; value: ReactNode; unit?: string; note?: ReactNode }) {
  return (
    <div className="stat">
      <Text type="label" color="secondary">
        {label}
      </Text>
      <div className="stat__value mono">
        {value}
        {unit && <span className="stat__unit">{unit}</span>}
      </div>
      {note && (
        <Text type="supporting" color="secondary">
          {note}
        </Text>
      )}
    </div>
  );
}
