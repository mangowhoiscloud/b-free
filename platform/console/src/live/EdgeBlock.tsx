/**
 * Edge: the demo app's public names (BRAVO_PUBLIC_FRONT_HOST, BRAVO_APP_ENVS), resolved live through a
 * public DNS-over-HTTPS JSON resolver. LB pools, tunnel status and the active environment need the Cloudflare
 * API (token only in the platform OpenBao, D-26); the console has no source for them, so it says so.
 */
import { useQuery } from '@tanstack/react-query';
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
      <p className="foot">
        출처: 공개 DNS-over-HTTPS <span className="mono">{dohUrl ?? '—'}</span>. 프록시된 레코드는 공개 DNS로 Cloudflare 주소만 보입니다.
      </p>
      <NotConnected
        title="LB · 터널 · 활성 환경 연결 안 됨"
        endpoint="Cloudflare API (플랫폼 경유)"
        reason="이 콘솔은 LB 풀 가중치, 터널 상태, 활성 환경을 읽는 출처가 없습니다. Cloudflare API 토큰은 플랫폼 OpenBao에만 있습니다(D-26)."
      />
    </Block>
  );
}
