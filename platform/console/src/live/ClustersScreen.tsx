/**
 * 클러스터 실시간: collector GET /v1/clusters (read-only kube get/list, no Secret data)
 * (the platform's own services are not probed here).
 */
import { VStack } from '@astryxdesign/core/Stack';
import { Block, DataTable, Facts, Screen, Val } from './ScreenFrame';
import { asArray, asRecord, Fresh, LiveGate, NotConnected, num, str, Tag, useLive } from './live';

interface Node { name: string; ready: boolean; kubelet_version: string | null; instance_type: string | null; zone: string | null; roles: string[] }
interface Workload { namespace: string; kind: string; name: string; desired: number; ready: number }
interface LzItem { component: string; namespace: string | null; present: boolean; ready: boolean | null; detail: string | null }
export interface Cluster {
  cluster_id: string; display_name: string | null; provider: string; context: string | null; status: string; error: string | null;
  server_version: string | null; nodes: Node[]; namespaces: { name: string; phase: string | null }[]; workloads: Workload[];
  pods: Record<string, number>; landing_zone: LzItem[];
}
export interface ClustersResp { observed_at: string | null; clusters: Cluster[]; not_configured: { cluster_id: string; reason: string }[] }

export function parseClusters(v: unknown): ClustersResp {
  const o = asRecord(v);
  return {
    observed_at: str(o.observed_at),
    clusters: asArray(o.clusters).map((c) => {
      const r = asRecord(c);
      return {
        cluster_id: str(r.cluster_id) ?? '?',
        display_name: str(r.display_name),
        provider: str(r.provider) ?? 'kubernetes',
        context: str(r.context),
        status: str(r.status) ?? 'unknown',
        error: str(r.error),
        server_version: str(r.server_version),
        nodes: asArray(r.nodes).map((n) => {
          const x = asRecord(n);
          return { name: str(x.name) ?? '?', ready: x.ready === true, kubelet_version: str(x.kubelet_version), instance_type: str(x.instance_type), zone: str(x.zone), roles: asArray(x.roles).map(String) };
        }),
        namespaces: asArray(r.namespaces).map((n) => ({ name: str(asRecord(n).name) ?? '?', phase: str(asRecord(n).phase) })),
        workloads: asArray(r.workloads).map((w) => {
          const x = asRecord(w);
          return { namespace: str(x.namespace) ?? '', kind: str(x.kind) ?? '', name: str(x.name) ?? '?', desired: num(x.desired) ?? 0, ready: num(x.ready) ?? 0 };
        }),
        pods: Object.fromEntries(Object.entries(asRecord(r.pods)).map(([k, v]) => [k, num(v) ?? 0])),
        landing_zone: asArray(r.landing_zone).map((l) => {
          const x = asRecord(l);
          return { component: str(x.component) ?? '?', namespace: str(x.namespace), present: x.present === true, ready: typeof x.ready === 'boolean' ? x.ready : null, detail: str(x.detail) };
        }),
      };
    }),
    not_configured: asArray(o.not_configured).map((n) => ({ cluster_id: str(asRecord(n).cluster_id) ?? '?', reason: str(asRecord(n).reason) ?? '' })),
  };
}

const PROVISION_STEPS = ['REQUESTED', 'PROVISIONING', 'BOOTSTRAPPING', 'VERIFYING', 'READY'] as const;

/** Distribution as the nodes report it (kubelet version suffix); the registry's `provider` only as fallback. */
export function distroOf(c: Pick<Cluster, 'nodes' | 'provider' | 'server_version'>): string {
  const v = [...c.nodes.map((n) => n.kubelet_version ?? ''), c.server_version ?? ''].join(' ');
  if (/\+rke2/.test(v)) return 'RKE2';
  if (/\+k3s/.test(v)) return 'K3s';
  if (/-eks-/.test(v)) return 'Amazon EKS';
  if (/-gke\./.test(v)) return 'GKE';
  return `${c.provider} (등록부 값)`;
}

/** Workloads whose name says they are an environment link (e.g. dst-db-link), as the collector sees them. */
export function linkWorkloads(d: ClustersResp) {
  return d.clusters.flatMap((c) => c.workloads.filter((w) => /(^|-)link(-|$)/.test(w.name)).map((w) => ({ cluster: c.cluster_id, ...w })));
}

const provisionLine = (reason: string) => (
  <span>
    <Val v={null} /> <span className="why">{reason} 단계: {PROVISION_STEPS.join(' → ')}</span>
  </span>
);

function ClusterCard({ c }: { c: Cluster }) {
  const ok = c.status === 'ok';
  const nodesReady = c.nodes.filter((n) => n.ready).length;
  const wlReady = c.workloads.filter((w) => w.ready >= w.desired).length;
  const pods = Object.values(c.pods).reduce((a, b) => a + b, 0);
  const lzPresent = c.landing_zone.filter((l) => l.present).length;
  return (
    <Block title={`${ok ? distroOf(c) : c.provider} · ${c.cluster_id}`} meta={<Tag variant={ok ? 'success' : 'error'} label={ok ? '정상' : '연결 안 됨'} />}>
      {!ok ? (
        <NotConnected endpoint={`GET /svc/collector/v1/clusters → ${c.cluster_id}`} reason={c.error ?? '이유 없음'} />
      ) : (
        <>
          <Facts
            items={[
              { label: 'kubectl 컨텍스트', value: <span className="mono">{c.context ?? '—'}</span> },
              { label: 'API 서버', value: <span className="mono">{c.server_version ?? '—'}</span> },
              { label: '노드 Ready', value: <span className="num">{nodesReady} / {c.nodes.length}</span> },
              { label: '워크로드 Ready', value: <span className="num">{wlReady} / {c.workloads.length}</span> },
              { label: '파드', value: <span className="num">{pods} ({Object.entries(c.pods).map(([k, v]) => `${k} ${v}`).join(', ') || '없음'})</span> },
              { label: '랜딩 존 구성 요소', value: <span className="num">{lzPresent} / {c.landing_zone.length} 있음</span> },
              { label: '프로비저닝 상태', value: provisionLine('콘솔이 읽는 프로비저닝 상태 출처가 없음.') },
            ]}
          />
          <DataTable
            rows={c.nodes}
            rowKey={(n) => n.name}
            empty="노드가 없습니다."
            cols={[
              { key: 'name', header: '노드', weight: 3, render: (n) => <span className="mono">{n.name}</span> },
              { key: 'ready', header: '상태', render: (n) => <Tag variant={n.ready ? 'success' : 'error'} label={n.ready ? 'Ready' : 'NotReady'} /> },
              { key: 'type', header: '유형 · 영역', weight: 2, render: (n) => <span className="mono">{[n.instance_type, n.zone].filter(Boolean).join(' · ') || '—'}</span> },
              { key: 'ver', header: 'kubelet', weight: 2, render: (n) => <span className="mono">{n.kubelet_version ?? '—'}</span> },
            ]}
          />
          <DataTable
            rows={c.workloads}
            rowKey={(w) => `${w.namespace}/${w.kind}/${w.name}`}
            empty="워크로드가 없습니다."
            cols={[
              { key: 'ns', header: '네임스페이스', weight: 2, render: (w) => <span className="mono">{w.namespace}</span> },
              { key: 'name', header: '워크로드', weight: 3, render: (w) => <span className="mono">{w.kind}/{w.name}</span> },
              { key: 's', header: '상태', render: (w) => (w.desired === 0 ? <span className="foot">대상 0</span> : <Tag variant={w.ready >= w.desired ? 'success' : 'warning'} label={w.ready >= w.desired ? '정상' : '준비 중'} />) },
              { key: 'ready', header: 'Ready', numeric: true, render: (w) => `${w.ready} / ${w.desired}` },
            ]}
          />
          <DataTable
            rows={c.landing_zone}
            rowKey={(l) => l.component}
            empty="랜딩 존 규칙이 없습니다."
            cols={[
              { key: 'c', header: '랜딩 존 구성 요소 (11-landing-zone §2)', weight: 2, render: (l) => l.component },
              { key: 'p', header: '상태', render: (l) => (!l.present ? <span className="foot">없음</span> : <Tag variant={l.ready ? 'success' : 'warning'} label={l.ready ? '정상' : l.ready === false ? '준비 중' : '있음'} />) },
              { key: 'd', header: '판단 근거', weight: 3, render: (l) => <span className="foot">{[l.namespace, l.detail].filter(Boolean).join(' · ') || '—'}</span> },
            ]}
          />
        </>
      )}
    </Block>
  );
}

export function ClustersScreen() {
  const live = useLive('collector', 'v1/clusters', 5000, parseClusters);
  return (
    <Screen
      title="클러스터 실시간"
      lead="환경마다 노드 · 네임스페이스 · 워크로드 준비 상태 · 랜딩 존 구성 요소를 수집기(읽기 전용 get/list)로 5초마다 읽습니다. Secret은 읽지 않습니다."
      aside={<Fresh at={live.updatedAt} label="수집" />}
    >
      <LiveGate live={live} title="클러스터 수집 연결 안 됨">
        {(d) => (
          <VStack gap={4}>
            <Block title="환경 사이 링크 (수집기가 보는 link 워크로드)">
              <DataTable
                rows={linkWorkloads(d)}
                rowKey={(w) => `${w.cluster}/${w.namespace}/${w.name}`}
                empty="수집기가 읽는 클러스터에 이름에 link가 들어간 워크로드가 없습니다. 플랫폼 클러스터의 링크는 수집기 등록부에 없으면 여기 보이지 않습니다."
                cols={[
                  { key: 'c', header: '클러스터', render: (w) => <span className="mono">{w.cluster}</span> },
                  { key: 'w', header: '워크로드', weight: 3, render: (w) => <span className="mono">{w.namespace}/{w.kind}/{w.name}</span> },
                  { key: 's', header: '상태', render: (w) => <Tag variant={w.desired > 0 && w.ready >= w.desired ? 'success' : 'warning'} label={w.desired > 0 && w.ready >= w.desired ? '정상' : '준비 중'} /> },
                  { key: 'r', header: 'Ready', numeric: true, render: (w) => `${w.ready} / ${w.desired}` },
                ]}
              />
            </Block>
            {d.clusters.map((c) => (
              <ClusterCard key={c.cluster_id} c={c} />
            ))}
            {d.not_configured.map((n) => (
              <Block key={n.cluster_id} title={`클러스터 · ${n.cluster_id}`} meta={<Tag variant="neutral" label="연결 안 됨" />}>
                <Facts
                  columns="single"
                  items={[
                    { label: '이유', value: n.reason },
                    { label: '시도한 곳', value: <span className="mono">GET /svc/collector/v1/clusters → not_configured</span> },
                    { label: '프로비저닝 상태', value: provisionLine('클러스터를 읽지 못함.') },
                  ]}
                />
              </Block>
            ))}
          </VStack>
        )}
      </LiveGate>
    </Screen>
  );
}
