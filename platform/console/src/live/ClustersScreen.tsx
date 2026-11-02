/**
 * 클러스터 실시간: collector GET /v1/clusters (read-only kube get/list, no Secret data)
 * (the platform's own services are not probed here).
 */
import { VStack } from '@astryxdesign/core/Stack';
import { Block, DataTable, Facts, HEALTH_TEXT, Screen, StatusBar, StatusText, Val, type Health } from './ScreenFrame';
import { useHashParam } from './hashQuery';
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
              { key: 'p', header: '상태', render: (l) => (!l.present ? <span className="foot">없음</span> : (l.ready === null ? <span className="meta">있음 · 준비 여부 판단 없음</span> : <StatusText h={l.ready ? 'ok' : 'warn'} />)) },
              { key: 'd', header: '판단 근거', weight: 3, render: (l) => <span className="foot">{[l.namespace, l.detail].filter(Boolean).join(' · ') || '—'}</span> },
            ]}
          />
        </>
      )}
    </Block>
  );
}

interface Row {
  id: string;
  distro: string | null;
  nodes: string | null;
  workloads: string | null;
  lz: string | null;
  health: Health;
  note: string;
  cluster: Cluster | null;
}

export function healthOf(c: Cluster): Health {
  if (c.status !== 'ok') return 'bad';
  const nodesOk = c.nodes.length > 0 && c.nodes.every((n) => n.ready);
  const wlOk = c.workloads.every((w) => w.ready >= w.desired);
  return nodesOk && wlOk ? 'ok' : 'warn';
}

function rowsOf(d: ClustersResp): Row[] {
  const rows: Row[] = d.clusters.map((c) => {
    const ok = c.status === 'ok';
    return {
      id: c.cluster_id,
      distro: ok ? distroOf(c) : c.provider,
      nodes: ok ? `${c.nodes.filter((n) => n.ready).length}/${c.nodes.length}` : null,
      workloads: ok ? `${c.workloads.filter((w) => w.ready >= w.desired).length}/${c.workloads.length}` : null,
      lz: ok ? `${c.landing_zone.filter((l) => l.present).length}/${c.landing_zone.length}` : null,
      health: healthOf(c),
      note: ok ? '' : c.error ?? '닿지 않음',
      cluster: c,
    };
  });
  for (const n of d.not_configured) rows.push({ id: n.cluster_id, distro: null, nodes: null, workloads: null, lz: null, health: 'none', note: n.reason, cluster: null });
  return rows;
}

export function ClustersScreen() {
  const live = useLive('collector', 'v1/clusters', 5000, parseClusters);
  const [hf, setHf] = useHashParam('health', '');
  const [sel, setSel] = useHashParam('env', '');
  return (
    <Screen title="클러스터" lead="환경마다 클러스터가 건강하고 원하는 상태인가" aside={<Fresh at={live.updatedAt} label="수집" />}>
      <LiveGate live={live} title="클러스터 수집 연결 안 됨">
        {(d) => {
          const rows = rowsOf(d);
          const counts = rows.reduce<Partial<Record<Health, number>>>((a, r) => ({ ...a, [r.health]: (a[r.health] ?? 0) + 1 }), {});
          const shown = rows.filter((r) => !hf || r.health === hf);
          const picked = rows.find((r) => r.id === sel) ?? null;
          return (
            <VStack gap={4}>
              <StatusBar counts={counts} label="클러스터 건강" />
              <div className="chips" role="group" aria-label="건강 필터">
                <button type="button" className="chip" aria-pressed={!hf} onClick={() => setHf(null)}>
                  전체 {rows.length}
                </button>
                {(['ok', 'warn', 'bad', 'none'] as Health[]).filter((h) => counts[h]).map((h) => (
                  <button key={h} type="button" className="chip" aria-pressed={hf === h} onClick={() => setHf(h)}>
                    {HEALTH_TEXT[h]} {counts[h]}
                  </button>
                ))}
              </div>
              <Block title="환경별 클러스터">
                <DataTable
                  rows={shown}
                  rowKey={(r) => r.id}
                  empty="이 필터에 맞는 클러스터가 없습니다."
                  cols={[
                    {
                      key: 'id', header: '환경', weight: 2,
                      render: (r) => (
                        <button type="button" className="rowbtn mono" aria-current={sel === r.id} onClick={() => setSel(sel === r.id ? null : r.id)}>
                          {r.id}
                        </button>
                      ),
                    },
                    { key: 'd', header: '배포판', render: (r) => r.distro ?? <Val v={null} /> },
                    { key: 'n', header: '노드', numeric: true, render: (r) => <Val v={r.nodes} /> },
                    { key: 'w', header: '준비 워크로드', numeric: true, px: 120, render: (r) => <Val v={r.workloads} /> },
                    { key: 'l', header: '랜딩 존', numeric: true, render: (r) => <Val v={r.lz} /> },
                    { key: 'h', header: '건강', weight: 2, render: (r) => <VStack gap={0}><StatusText h={r.health} />{r.note && <span className="meta">{r.note}</span>}</VStack> },
                    { key: 's', header: '동기화', render: () => <span className="meta" title="원하는 상태와 비교할 상태 원장이 콘솔에 열려 있지 않습니다">○ 연결 안 됨</span> },
                  ]}
                />
                <p className="meta">건강 = API 응답 · 노드 Ready · 워크로드 준비(수집기 get/list). 동기화 = 원하는 상태와 같은가(Argo CD식 두 번째 축)로, 상태 원장이 열리기 전까지 연결 안 됨입니다. 줄의 환경 이름을 누르면 아래에 자세히 보입니다.</p>
              </Block>
              {picked?.cluster ? <ClusterCard c={picked.cluster} /> : picked ? (
                <Block title={`클러스터 · ${picked.id}`}>
                  <NotConnected endpoint="GET /svc/collector/v1/clusters → not_configured" reason={picked.note} />
                </Block>
              ) : null}
              <Block title="환경 사이 링크 · 수집기가 보는 link 워크로드">
                <DataTable
                  rows={linkWorkloads(d)}
                  rowKey={(w) => `${w.cluster}/${w.namespace}/${w.name}`}
                  empty="수집기가 읽는 클러스터에 이름에 link가 들어간 워크로드가 없습니다."
                  cols={[
                    { key: 'c', header: '클러스터', render: (w) => <span className="mono">{w.cluster}</span> },
                    { key: 'w', header: '워크로드', weight: 3, render: (w) => <span className="mono">{w.namespace}/{w.kind}/{w.name}</span> },
                    { key: 's', header: '상태', render: (w) => <StatusText h={w.desired > 0 && w.ready >= w.desired ? 'ok' : 'warn'} /> },
                    { key: 'r', header: 'Ready', numeric: true, render: (w) => `${w.ready}/${w.desired}` },
                  ]}
                />
                <p className="meta">플랫폼 클러스터의 링크 서비스는 수집기 등록부에 없으면 여기 보이지 않습니다.</p>
              </Block>
            </VStack>
          );
        }}
      </LiveGate>
    </Screen>
  );
}
