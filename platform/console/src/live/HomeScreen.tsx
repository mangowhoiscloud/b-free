/**
 * 홈: the first screen after login, no plan ID needed.
 * - who is logged in (GET /v1/me, or the dev token in local runs)
 * - environments (GET /v1/environments) joined with live cluster status from the collector
 * - plans (GET /v1/plans) with state, source → target and pending approval; a row opens the plan
 * - operators start a migration (POST /v1/plans); approvers see what waits for them
 * Each read that fails shows "연결 안 됨" with the endpoint and reason. No rows are invented.
 */
import { useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Button } from '@astryxdesign/core/Button';
import { Link } from '@astryxdesign/core/Link';
import { Selector } from '@astryxdesign/core/Selector';
import { HStack, VStack } from '@astryxdesign/core/Stack';
import { TextInput } from '@astryxdesign/core/TextInput';
import { ApiError, errorText } from '../api/client';
import type { EnvSummary, PlanSummary } from '../api/home';
import { fmtLatency, fmtMoney, fmtTs, shortId } from '../lib/format';
import { extraNote, extraOf } from './costExtra';
import { idemKey, releaseIdemKey } from '../lib/idem';
import { useApi, type Session } from '../state';
import { reasonText } from '../components/StateProgress';
import { distroOf, parseClusters, type Cluster, type ClustersResp } from './ClustersScreen';
import { Block, DataTable, KpiRow, Screen, StatusBar, StatusText, Val, type Health } from './ScreenFrame';
import { Fresh, liveReason, NotConnected, Tag, useConfig, useLive } from './live';

const STAGE_TEXT: Record<string, string> = { change: '변경 승인', cutover: '컷오버 승인', failback: '되돌리기 승인' };
const ALERT = new Set(['SAFE_HOLD', 'ABORT_PRE_WRITE']);

function apiReason(err: unknown): string {
  if (err instanceof ApiError && (err.status === 404 || err.status === 405)) return `API가 이 경로를 모릅니다(${err.status}).`;
  return errorText(err);
}

function planHref(id: string) {
  return `${import.meta.env.BASE_URL}plans/${encodeURIComponent(id)}#/plan`;
}

interface EnvRow {
  key: string;
  name: string;
  role: string | null;
  distro: string | null;
  inApi: boolean;
  cluster: 'ok' | 'down' | 'none' | 'unknown';
  clusterNote: string;
  nodes: string | null;
  apps: string[];
  secret: string | null;
}

const SYSTEM_NS = /^(kube-|cattle-|calico-|tigera-|cilium|longhorn|cert-manager|bravo-link$|amazon-|aws-)/;

/** App candidates as the collector sees them: non-system namespaces that run workloads. */
export function appsOf(c: Cluster | undefined): string[] {
  if (!c) return [];
  return [...new Set(c.workloads.map((w) => w.namespace).filter((ns) => !SYSTEM_NS.test(ns)))].sort();
}

function liveState(c: Cluster): Pick<EnvRow, 'cluster' | 'clusterNote' | 'nodes' | 'distro'> {
  const ok = c.status === 'ok';
  return {
    cluster: ok ? 'ok' : 'down',
    clusterNote: ok ? `${c.cluster_id} 응답` : c.error ?? '닿지 않음',
    nodes: ok ? `${c.nodes.filter((n) => n.ready).length} / ${c.nodes.length}` : null,
    distro: ok ? distroOf(c) : c.provider,
  };
}

function envRows(envs: EnvSummary[] | undefined, clusters: ClustersResp | undefined): EnvRow[] {
  const rows: EnvRow[] = [];
  const used = new Set<string>();
  for (const e of envs ?? []) {
    const keys = [e.name, e.id, e.context].filter((k): k is string => !!k);
    const c = clusters?.clusters.find((x) => keys.includes(x.cluster_id) || (x.context !== null && keys.includes(x.context)));
    const nc = clusters?.not_configured.find((n) => keys.includes(n.cluster_id));
    if (c) used.add(c.cluster_id);
    if (nc) used.add(nc.cluster_id);
    const base = { key: `api:${e.id}`, name: e.name ?? e.id, role: e.role, inApi: true, apps: e.apps.length ? e.apps : appsOf(c), secret: e.secretStatus };
    if (c) rows.push({ ...base, ...liveState(c) });
    else
      rows.push({
        ...base,
        // No live collector row: show the API's own join (cluster.status / reason) as it reports it.
        cluster: e.clusterStatus === 'ok' ? 'ok' : e.clusterStatus === 'unreachable' ? 'down' : 'none',
        clusterNote: nc?.reason ?? e.clusterReason ?? (e.clusterStatus ? `API: ${e.clusterStatus}` : '수집기에서 이 환경을 찾지 못함'),
        nodes: e.clusterNodes !== null ? `${e.clusterNodes}개` : null,
        distro: e.provider,
      });
  }
  for (const c of clusters?.clusters ?? []) {
    if (used.has(c.cluster_id)) continue;
    rows.push({ key: `col:${c.cluster_id}`, name: c.cluster_id, role: null, inApi: false, apps: appsOf(c), secret: null, ...liveState(c) });
  }
  for (const n of clusters?.not_configured ?? []) {
    if (used.has(n.cluster_id)) continue;
    rows.push({ key: `nc:${n.cluster_id}`, name: n.cluster_id, role: null, distro: null, inApi: false, cluster: 'none', clusterNote: n.reason, nodes: null, apps: [], secret: null });
  }
  return rows;
}


function NewMigration({ envs, clusters, token, onCreated }: { envs: EnvSummary[]; clusters: ClustersResp | undefined; token: string; onCreated: (id: string) => void }) {
  const api = useApi();
  const [source, setSource] = useState('');
  const [target, setTarget] = useState('');
  const [app, setApp] = useState('');
  const srcEnv = envs.find((e) => e.id === source);
  const srcCluster = srcEnv ? clusters?.clusters.find((c) => [srcEnv.name, srcEnv.id].includes(c.cluster_id)) : undefined;
  const appChoices = srcEnv ? (srcEnv.apps.length ? srcEnv.apps : appsOf(srcCluster)) : [];
  const scope = `home-create:${source}:${target}:${app.trim()}`;
  const create = useMutation({
    mutationFn: () => api.createPlan(token, { workload: app.trim(), source_environment_id: source, target_environment_id: target }, idemKey(scope)),
    onSuccess: (r) => {
      releaseIdemKey(scope);
      onCreated(r.plan_id);
    },
    onError: (err) => {
      if (!(err instanceof ApiError && err.status === 0)) releaseIdemKey(scope);
    },
  });
  const options = envs.map((e) => ({ value: e.id, label: `${e.name ?? e.id}${e.role ? ` · ${e.role}` : ''}` }));
  const same = source !== '' && source === target;
  const valid = source && target && !same && /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/.test(app.trim());
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (valid && !create.isPending) create.mutate();
      }}
    >
      <VStack gap={3}>
        <HStack gap={3} wrap="wrap" vAlign="end">
          <Selector label="출발 환경" options={options} value={source} onChange={setSource} placeholder="환경 고르기…" isRequired width={260} />
          <Selector label="목적 환경" options={options} value={target} onChange={setTarget} placeholder="환경 고르기…" isRequired width={260} status={same ? { type: 'error', message: '출발과 목적이 같습니다.' } : undefined} />
          {appChoices.length > 0 ? (
            <Selector label="앱" options={appChoices.map((a) => ({ value: a, label: a }))} value={app} onChange={setApp} placeholder="앱 고르기…" isRequired width={200} />
          ) : (
            <TextInput label="앱 (워크로드 이름)" value={app} onChange={(v) => setApp(v)} placeholder="예: chat…" isRequired width={200} htmlName="workload" />
          )}
          <Button type="submit" variant="primary" label="새 이전 시작" isDisabled={!valid} isLoading={create.isPending} />
        </HStack>
        {create.error && (
          <p className="nc" role="alert">
            <Tag variant="error" label="만들지 못함" />
            <span>{apiReason(create.error)}</span>
          </p>
        )}
        <p className="foot">앱 후보는 수집기가 출발 클러스터에서 본 업무 네임스페이스입니다. 계획을 만들면 그 계획 화면으로 갑니다. 게이트 · 승인 · 컷오버는 계획 화면에서 이어집니다. 승인은 다른 사람(승인자)이 합니다.</p>
      </VStack>
    </form>
  );
}

const DONE = new Set(['WATCH']);

/** GET /v1/cost totals for the KPI (same defensive reading as the cost screen). */
function useCostTotal(apiBase: string) {
  return useQuery({
    queryKey: ['cost-kpi'],
    retry: 0,
    refetchInterval: 60_000,
    queryFn: async () => {
      const res = await fetch(`${apiBase}/v1/cost`, { credentials: 'same-origin', headers: { Accept: 'application/json' } });
      if (!res.ok) throw new Error(`GET /v1/cost → ${res.status}`);
      return extraOf(await res.json());
    },
  });
}

export function HomeScreen({ session, onOpenPlan }: { session: Session; onOpenPlan: (id: string) => void }) {
  const api = useApi();
  const { apiBase } = useConfig();
  const token = session.user?.token ?? '';
  const plans = useQuery({ queryKey: ['plans', token], queryFn: () => api.listPlans(token), refetchInterval: 5000, retry: 0 });
  const envs = useQuery({ queryKey: ['environments', token], queryFn: () => api.listEnvironments(token), refetchInterval: 10_000, retry: 0 });
  const clusters = useLive('collector', 'v1/clusters', 5000, parseClusters);
  const cost = useCostTotal(apiBase);
  // Last write pause: the newest plan that reached CUTOVER or later carries the run record.
  const ranPlan = (plans.data ?? []).find((p) => p.state === 'CUTOVER' || p.state === 'WATCH' || p.state === 'SAFE_HOLD');
  const ran = useQuery({ queryKey: ['plan', ranPlan?.plan_id, 'kpi'], enabled: Boolean(ranPlan), queryFn: () => api.getPlan(token, ranPlan?.plan_id ?? ''), retry: 0, staleTime: 15_000 });

  const me = session.me.data;
  const role = me?.role ?? session.user?.role ?? null;
  const rows = envRows(envs.data, clusters.data);
  const waiting = (plans.data ?? []).filter((p) => p.awaiting);
  const active = (plans.data ?? []).filter((p) => p.state && !DONE.has(p.state));
  const counts = rows.reduce<Partial<Record<Health, number>>>((acc, r) => {
    const h: Health = r.cluster === 'ok' ? 'ok' : r.cluster === 'down' ? 'bad' : 'none';
    acc[h] = (acc[h] ?? 0) + 1;
    return acc;
  }, {});
  const pause = ran.data?.run?.write_pause_ms ?? null;

  const planTable = (list: PlanSummary[], empty: string) => (
    <DataTable
      rows={list}
      rowKey={(p) => p.plan_id}
      empty={empty}
      cols={[
        {
          key: 'id', header: '계획', weight: 2,
          render: (p) => (
            <Link href={planHref(p.plan_id)} onClick={(e) => { if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return; e.preventDefault(); onOpenPlan(p.plan_id); }}>
              <span className="mono">{shortId(p.plan_id, 8)}</span>
            </Link>
          ),
        },
        { key: 'app', header: '앱', render: (p) => <span className="mono">{p.workload ?? '—'}</span> },
        {
          key: 's', header: '상태', weight: 2,
          render: (p) =>
            p.state ? (
              <VStack gap={0}>
                <StatusText h={ALERT.has(p.state) ? 'bad' : p.stateReason ? 'warn' : DONE.has(p.state) ? 'ok' : 'run'} text={p.stateReason ? `${p.state} · 멈춤` : p.state} />
                {p.stateReason && <span className="meta">{reasonText(p.stateReason)}</span>}
              </VStack>
            ) : (
              <Val v={null} />
            ),
        },
        { key: 'st', header: '출발 · 목적', weight: 3, render: (p) => <span className="mono">{p.source ?? '—'} · {p.target ?? '—'}</span> },
        { key: 'a', header: '승인 대기', render: (p) => (p.awaiting ? <StatusText h="warn" text={STAGE_TEXT[p.awaiting] ?? p.awaiting} /> : <span className="meta">없음</span>) },
        { key: 't', header: '생성', numeric: true, px: 180, render: (p) => <span className="meta">{fmtTs(p.created_at)}</span> },
      ]}
    />
  );

  return (
    <Screen title="홈" lead="이전은 어디까지 왔고, 내가 할 일이 있는가">
      {envs.error && clusters.error ? (
        <NotConnected title="환경 상태 연결 안 됨" endpoint="GET /v1/environments · /svc/collector/v1/clusters" reason={apiReason(envs.error)} />
      ) : (
        <StatusBar counts={counts} label="환경 상태" />
      )}

      <KpiRow
        label="요약"
        items={[
          {
            label: '진행 중 계획',
            value: plans.error ? '—' : plans.data ? <span className="num">{active.length}</span> : '…',
            note: plans.error ? `연결 안 됨 · GET /v1/plans · ${apiReason(plans.error)}` : active[0]?.state ? `가장 최근 ${active[0].state}` : '진행 중인 계획 없음',
          },
          {
            label: '승인 대기',
            value: plans.error ? '—' : plans.data ? <span className="num">{waiting.length}</span> : '…',
            note: plans.error ? '연결 안 됨 · GET /v1/plans' : waiting[0]?.awaiting ? `${STAGE_TEXT[waiting[0].awaiting] ?? waiting[0].awaiting} · ${shortId(waiting[0].plan_id)}` : '없음',
          },
          {
            label: '마지막 쓰기 정지',
            value: pause !== null ? fmtLatency(pause) : '—',
            note: !ranPlan ? (plans.error ? '연결 안 됨 · GET /v1/plans' : '컷오버한 계획 없음') : ran.error ? `연결 안 됨 · GET /v1/plans/${shortId(ranPlan.plan_id)}` : `계획 ${shortId(ranPlan.plan_id)} · ${ranPlan.state}`,
          },
          {
            label: '이전 중 추가 비용',
            value: cost.data?.extra != null ? fmtMoney(cost.data.extra, cost.data.currency) : '—',
            note: extraNote(cost.data, cost.error),
          },
        ]}
      />

      {role === 'operator' && (
        <Block title="새 이전 시작">
          {envs.error ? (
            <NotConnected endpoint="GET /v1/environments" reason={`환경 목록이 있어야 출발 · 목적을 고를 수 있습니다. ${apiReason(envs.error)}`} />
          ) : !envs.data ? (
            <p className="meta">환경 목록을 불러오는 중…</p>
          ) : envs.data.length < 2 ? (
            <p className="meta">등록된 환경이 {envs.data.length}개라 이전을 만들 수 없습니다. 출발과 목적 환경이 모두 등록되어야 합니다.</p>
          ) : (
            <NewMigration envs={envs.data} clusters={clusters.data} token={token} onCreated={onOpenPlan} />
          )}
        </Block>
      )}

      <Block title={role === 'approver' ? `내 승인 대기 ${waiting.length}건 · 전체 계획` : `계획${plans.data ? ` ${plans.data.length}건` : ''}`} meta={<Fresh at={plans.dataUpdatedAt} />}>
        {plans.error ? (
          <NotConnected endpoint="GET /v1/plans" reason={apiReason(plans.error)} />
        ) : (
          planTable(role === 'approver' ? [...waiting, ...(plans.data ?? []).filter((p) => !p.awaiting)] : plans.data ?? [], plans.isLoading ? '불러오는 중…' : '아직 계획이 없습니다.')
        )}
        {role === 'approver' && <p className="meta">승인은 계획 화면의 승인 칸에서 합니다. 계획을 만든 사람이나 승인을 요청한 사람은 승인할 수 없습니다(직무 분리).</p>}
      </Block>

      <Block title="환경" meta={<Fresh at={Math.max(envs.dataUpdatedAt, clusters.updatedAt)} />}>
        {envs.error ? <NotConnected title="API 환경 목록 연결 안 됨" endpoint="GET /v1/environments" reason={apiReason(envs.error)} /> : null}
        {clusters.error ? <NotConnected title="클러스터 상태 연결 안 됨" endpoint={clusters.endpoint} reason={liveReason(clusters.error)} lastOk={clusters.updatedAt || undefined} /> : null}
        <DataTable
          rows={rows}
          rowKey={(r) => r.key}
          empty={envs.isLoading || clusters.isLoading ? '불러오는 중…' : '보여 줄 환경이 없습니다(API 목록과 수집기 모두 비어 있음).'}
          cols={[
            { key: 'n', header: '환경', weight: 2, render: (r) => <span className="mono">{r.name}</span> },
            { key: 'r', header: '역할', render: (r) => (r.role ? r.role : <Val v={null} />) },
            { key: 'p', header: '배포판', render: (r) => (r.distro ? r.distro : <Val v={null} />) },
            { key: 'nodes', header: '노드', numeric: true, render: (r) => <Val v={r.nodes} /> },
            { key: 'c', header: '클러스터', weight: 2, render: (r) => <VStack gap={0}><StatusText h={r.cluster === 'ok' ? 'ok' : r.cluster === 'down' ? 'bad' : 'none'} />{r.cluster !== 'ok' && <span className="meta">{r.clusterNote}</span>}</VStack> },
            { key: 'sec', header: '비밀 참조', render: (r) => (r.secret ? <StatusText h={r.secret === 'resolved' ? 'ok' : 'warn'} text={r.secret} /> : <Val v={null} />) },
          ]}
        />
        <p className="meta">출처: 환경 목록 GET /v1/environments, 클러스터 · 노드 · 배포판은 수집기 GET /v1/clusters(배포판은 노드 kubelet 버전에서 읽음).</p>
      </Block>
    </Screen>
  );
}
