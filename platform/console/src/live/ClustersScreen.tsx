/**
 * 클러스터 실시간: collector GET /v1/clusters (read-only kube get/list, no Secret data)
 * plus health probes of the pilot Compose platform services.
 */
import { Banner } from '@astryxdesign/core/Banner';
import { Grid } from '@astryxdesign/core/Grid';
import { HStack, VStack } from '@astryxdesign/core/Stack';
import { Text } from '@astryxdesign/core/Text';
import { useQuery } from '@tanstack/react-query';
import { Block, DataTable, Screen, Stat } from './ScreenFrame';
import { asArray, asRecord, Fresh, LiveGate, liveReason, NotConnected, num, OkDot, str, Tag, useConfig, useLive } from './live';

interface Node { name: string; ready: boolean; kubelet_version: string | null; instance_type: string | null; zone: string | null; roles: string[] }
interface Workload { namespace: string; kind: string; name: string; desired: number; ready: number }
interface LzItem { component: string; namespace: string | null; present: boolean; ready: boolean | null; detail: string | null }
interface Cluster {
  cluster_id: string; display_name: string | null; provider: string; context: string | null; status: string; error: string | null;
  server_version: string | null; nodes: Node[]; namespaces: { name: string; phase: string | null }[]; workloads: Workload[];
  pods: Record<string, number>; landing_zone: LzItem[];
}
interface ClustersResp { observed_at: string | null; clusters: Cluster[]; not_configured: { cluster_id: string; reason: string }[] }

function parse(v: unknown): ClustersResp {
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
const PROVIDER_LABEL: Record<string, string> = { eks: 'Amazon EKS', k3s: 'K3s 온프렘', gke: 'GKE', kubernetes: 'Kubernetes' };

function ProvisionMachine({ c }: { c: Cluster }) {
  const nodesReady = c.nodes.filter((n) => n.ready).length;
  const lzPresent = c.landing_zone.filter((l) => l.present).length;
  const lzReady = c.landing_zone.filter((l) => l.ready === true).length;
  return (
    <VStack gap={2}>
      <ol className="pipeline" aria-label="프로비저닝 상태 기계 (11-landing-zone §4)">
        {PROVISION_STEPS.map((s) => (
          <li key={s} className="pipeline__step pipeline__step--unknown">
            <span className="mono">{s}</span>
          </li>
        ))}
      </ol>
      <Text type="supporting" color="secondary" as="p">
        현재 단계: <b>연결 안 됨</b> · 이유: 프로비저닝 상태 원장(deploy/landing-zone)이 아직 API로 열려 있지 않아 단계를 표시하지 않습니다. 관측 근거만 보입니다 — API 응답{' '}
        {c.status === 'ok' ? '있음' : '없음'}, 노드 Ready {nodesReady}/{c.nodes.length}, 랜딩 존 구성 요소 존재 {lzPresent}/{c.landing_zone.length} (Ready {lzReady}).
      </Text>
    </VStack>
  );
}

function ClusterCard({ c }: { c: Cluster }) {
  const ok = c.status === 'ok';
  const notReady = c.workloads.filter((w) => w.ready < w.desired);
  return (
    <Block
      title={`${PROVIDER_LABEL[c.provider] ?? c.provider} · ${c.cluster_id}`}
      meta={
        <>
          <OkDot ok={ok} label={ok ? '응답함' : '닿지 않음'} />
          <Tag variant={ok ? 'success' : 'error'} label={ok ? '연결됨' : '연결 안 됨'} />
          {c.server_version && <Tag variant="neutral" label={c.server_version} />}
          {c.context && <Tag variant="info" label={`context ${c.context}`} />}
        </>
      }
    >
      {!ok && <NotConnected endpoint={`GET /svc/collector/v1/clusters → ${c.cluster_id}`} reason={c.error ?? '이유 없음'} />}
      {ok && (
        <>
          <Grid columns={{ minWidth: 140 }} gap={3}>
            <Stat label="노드 Ready" value={`${c.nodes.filter((n) => n.ready).length}/${c.nodes.length}`} />
            <Stat label="네임스페이스" value={c.namespaces.length} />
            <Stat label="워크로드 Ready" value={`${c.workloads.length - notReady.length}/${c.workloads.length}`} />
            <Stat label="파드" value={Object.values(c.pods).reduce((a, b) => a + b, 0)} note={Object.entries(c.pods).map(([k, v]) => `${k} ${v}`).join(' · ') || '없음'} />
          </Grid>
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
              {
                key: 'ready',
                header: 'Ready',
                render: (w) => <Tag variant={w.desired === 0 ? 'neutral' : w.ready >= w.desired ? 'success' : 'warning'} label={`${w.ready}/${w.desired}`} />,
              },
            ]}
          />
          <VStack gap={2}>
            <Text type="label">랜딩 존 구성 요소 (11-landing-zone §2)</Text>
            <DataTable
              rows={c.landing_zone}
              rowKey={(l) => l.component}
              empty="랜딩 존 규칙이 없습니다."
              cols={[
                { key: 'c', header: '구성 요소', weight: 2, render: (l) => l.component },
                { key: 'p', header: '상태', render: (l) => <Tag variant={!l.present ? 'neutral' : l.ready ? 'success' : 'warning'} label={!l.present ? '없음' : l.ready ? 'Ready' : l.ready === false ? '준비 안 됨' : '있음'} /> },
                { key: 'd', header: '근거', weight: 3, render: (l) => <Text type="supporting" color="secondary">{[l.namespace, l.detail].filter(Boolean).join(' · ') || '—'}</Text> },
              ]}
            />
          </VStack>
        </>
      )}
      <ProvisionMachine c={c} />
    </Block>
  );
}

const PLATFORM_PROBES: { name: string; label: string; path: string; via: 'svc' | 'api' }[] = [
  { name: 'api', label: '플랫폼 API', path: 'healthz', via: 'api' },
  { name: 'executor', label: 'Executor', path: 'healthz', via: 'svc' },
  { name: 'gate', label: '게이트', path: 'healthz', via: 'svc' },
  { name: 'collector', label: '수집기', path: 'healthz', via: 'svc' },
  { name: 'prometheus', label: 'Prometheus', path: '-/ready', via: 'svc' },
  { name: 'app-src', label: '데모 앱 src', path: 'healthz', via: 'svc' },
  { name: 'app-dst', label: '데모 앱 dst', path: 'healthz', via: 'svc' },
];

function probeUrl(p: (typeof PLATFORM_PROBES)[number], svcBase: string, apiBase: string) {
  return p.via === 'api' ? `${apiBase}/${p.path}` : `${svcBase}/${p.name}/${p.path}`;
}

export function PlatformCompose() {
  const { svcBase, apiBase } = useConfig();
  const q = useQuery({
    queryKey: ['platform-probes', svcBase, apiBase],
    refetchInterval: 4000,
    retry: 0,
    queryFn: async () =>
      Promise.all(
        PLATFORM_PROBES.map(async (p) => {
          const url = probeUrl(p, svcBase, apiBase);
          const t0 = performance.now();
          try {
            const res = await fetch(url, { credentials: 'omit', cache: 'no-store' });
            return { ...p, url, ok: res.ok, detail: `${res.status}`, ms: Math.round(performance.now() - t0) };
          } catch (e) {
            return { ...p, url, ok: false, detail: liveReason(e), ms: null };
          }
        }),
      ),
  });
  return (
    <Block title="플랫폼 · 파일럿 Compose" meta={<Fresh at={q.dataUpdatedAt} />}>
      <Text type="supporting" color="secondary" as="p">
        컨트롤 플랫폼은 노트북의 Docker Compose(bravo-pilot)에서 돕니다. 각 서비스의 상태 확인 주소를 4초마다 읽습니다.
      </Text>
      <DataTable
        rows={q.data ?? []}
        rowKey={(r) => r.name}
        empty="확인 중입니다."
        cols={[
          { key: 'n', header: '서비스', weight: 2, render: (r) => r.label },
          { key: 's', header: '상태', render: (r) => <Tag variant={r.ok ? 'success' : 'error'} label={r.ok ? '정상' : '연결 안 됨'} /> },
          { key: 'u', header: '확인 주소', weight: 3, render: (r) => <span className="mono">GET {r.via === 'api' ? r.url : `/svc/${r.name}/${r.path}`}</span> },
          { key: 'd', header: '응답', render: (r) => <span className="mono">{r.detail}{r.ms !== null ? ` · ${r.ms}ms` : ''}</span> },
        ]}
      />
    </Block>
  );
}

export function ClustersScreen() {
  const live = useLive('collector', 'v1/clusters', 5000, parse);
  return (
    <Screen
      title="클러스터 실시간"
      lead="환경마다 노드 · 네임스페이스 · 워크로드 준비 상태 · 랜딩 존 구성 요소를 수집기(읽기 전용 get/list)로 5초마다 읽습니다. Secret은 읽지 않습니다."
      aside={<Fresh at={live.updatedAt} label="수집" />}
    >
      <Banner
        status="info"
        container="card"
        title="환경 사이 링크: 임시 링크(D-16 전)"
        description="플랫폼(노트북)의 kubectl port-forward로 잇습니다. 목적지 K3s(Lima)는 host.lima.internal로 노트북 포트에 닿습니다. D-16 목표인 Cloudflare Tunnel + Access 서비스 토큰은 Zero Trust가 아직 꺼져 있어 쓰지 않습니다(CLOUD-E2E-CONTRACT §7)."
      />
      <LiveGate live={live} title="클러스터 수집 연결 안 됨">
        {(d) => (
          <VStack gap={4}>
            {d.clusters.map((c) => (
              <ClusterCard key={c.cluster_id} c={c} />
            ))}
            {d.not_configured.map((n) => (
              <Block key={n.cluster_id} title={`${n.cluster_id.includes('k3s') ? 'K3s 온프렘' : '클러스터'} · ${n.cluster_id}`} meta={<Tag variant="neutral" label="연결 안 됨" />}>
                <NotConnected endpoint={`GET /svc/collector/v1/clusters → not_configured`} reason={n.reason} />
                <HStack gap={2} wrap="wrap">
                  {PROVISION_STEPS.map((s) => (
                    <Tag key={s} variant="neutral" label={s} />
                  ))}
                </HStack>
                <Text type="supporting" color="secondary" as="p">
                  프로비저닝 단계: 연결 안 됨 · 수집기 등록부의 kubeconfig 참조가 비어 있어 클러스터를 읽지 못했습니다. 온프렘 갈래가 K3s(Lima) 컨텍스트를 보고하면 그 kubeconfig를 수집기에 읽기 전용으로 연결합니다.
                </Text>
              </Block>
            ))}
          </VStack>
        )}
      </LiveGate>
      <PlatformCompose />
    </Screen>
  );
}
