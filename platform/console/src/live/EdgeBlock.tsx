/**
 * Edge: the public name and the per-environment names (CLOUD-E2E-CONTRACT §6), resolved live through a
 * public DNS-over-HTTPS JSON resolver. Tunnel status and LB weights need the Cloudflare API, whose token
 * lives only in the platform OpenBao (D-26), so they show "연결 안 됨" / "LB 구독 필요".
 */
import { useQuery } from '@tanstack/react-query';
import { Text } from '@astryxdesign/core/Text';
import { Block, DataTable } from './ScreenFrame';
import { asArray, asRecord, Fresh, getJson, liveReason, NotConnected, num, str, Tag, useConfig } from './live';

interface Lookup { name: string; status: number | null; answers: { type: number | null; data: string | null }[]; error: string | null }

const TYPE: Record<number, string> = { 1: 'A', 5: 'CNAME', 28: 'AAAA' };

export function EdgeBlock() {
  const { publicFrontHost, dohUrl, appEnvs } = useConfig();
  const names = [publicFrontHost, ...appEnvs.map((e) => e.host)];
  const q = useQuery({
    queryKey: ['doh', dohUrl, ...names],
    enabled: Boolean(dohUrl),
    refetchInterval: 15_000,
    retry: 0,
    queryFn: () =>
      Promise.all(
        names.map(async (name): Promise<Lookup> => {
          try {
            const o = asRecord(await getJson(`${dohUrl}?name=${encodeURIComponent(name)}&type=A`, 6000, 'application/dns-json'));
            return { name, status: num(o.Status), answers: asArray(o.Answer).map((a) => ({ type: num(asRecord(a).type), data: str(asRecord(a).data) })), error: null };
          } catch (e) {
            return { name, status: null, answers: [], error: liveReason(e) };
          }
        }),
      ),
  });
  return (
    <Block title="앞단 · 데모 앱 공개 이름" meta={<Fresh at={q.dataUpdatedAt} label="DNS 확인" />}>
      {!dohUrl ? (
        <NotConnected endpoint="DNS-over-HTTPS" reason="BRAVO_DOH_URL이 비어 있어 이름 확인을 하지 않습니다" />
      ) : (
        <DataTable
          rows={q.data ?? []}
          rowKey={(r) => r.name}
          empty="DNS를 확인하는 중…"
          cols={[
            { key: 'n', header: '이름', weight: 2, render: (r) => <span className="mono">{r.name}</span> },
            {
              key: 's',
              header: 'DNS',
              render: (r) =>
                r.error ? <Tag variant="error" label="확인 실패" /> : r.answers.length ? <Tag variant="success" label="응답 있음" /> : <Tag variant="neutral" label="레코드 없음" />,
            },
            {
              key: 'a',
              header: '응답',
              weight: 3,
              render: (r) => (
                <span className="mono">
                  {r.error ?? (r.answers.length ? r.answers.map((a) => `${TYPE[a.type ?? 0] ?? a.type} ${a.data}`).join(' · ') : `status ${r.status ?? '—'} · 아직 만들어지지 않음`)}
                </span>
              ),
            },
          ]}
        />
      )}
      <Text type="supporting" color="secondary" as="p">
        출처: 공개 DNS-over-HTTPS <span className="mono">{dohUrl ?? '—'}</span>. 대표 이름은 LB 구독 전까지 활성 환경 터널을 가리키는 CNAME 하나입니다. 우리 플랫폼은 b-free입니다(CLOUD-E2E-CONTRACT §6).
      </Text>
      <DataTable
        rows={[
          { k: '활성 환경', v: '연결 안 됨', why: 'Cloudflare 레코드 내용(터널 ID)을 읽는 플랫폼 읽기 전용 경로가 없습니다. 프록시된 레코드는 공개 DNS로 터널 대상을 볼 수 없습니다' },
          { k: '터널 상태', v: '연결 안 됨', why: 'Cloudflare API 토큰은 플랫폼 OpenBao에만 있고(D-26) 터널 상태를 읽어 주는 플랫폼 엔드포인트가 아직 없습니다' },
          { k: 'LB 가중치', v: 'LB 구독 필요', why: 'Cloudflare Load Balancing 구독 전에는 가중치가 없습니다. 전환은 CNAME 교체입니다' },
        ]}
        rowKey={(r) => r.k}
        empty=""
        cols={[
          { key: 'k', header: '항목', render: (r) => r.k },
          { key: 'v', header: '상태', render: (r) => <Tag variant={r.v === 'LB 구독 필요' ? 'warning' : 'neutral'} label={r.v} /> },
          { key: 'w', header: '이유', weight: 4, render: (r) => <Text type="supporting" color="secondary">{r.why}</Text> },
        ]}
      />
    </Block>
  );
}
