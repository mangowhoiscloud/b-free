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
import { fmtDateTime, shortId } from '../lib/format';
import { idemKey, releaseIdemKey } from '../lib/idem';
import { useApi, type Session } from '../state';
import { parseClusters, type ClustersResp } from './ClustersScreen';
import { Block, DataTable, Facts, Screen, Val } from './ScreenFrame';
import { Fresh, liveReason, NotConnected, Tag, useLive } from './live';

const ROLE_TEXT: Record<string, string> = { operator: '운영자', approver: '승인자', viewer: '조회자' };
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
  provider: string | null;
  apiStatus: string | null;
  inApi: boolean;
  cluster: 'ok' | 'down' | 'none' | 'unknown';
  clusterNote: string;
  nodes: string | null;
  apps: string[];
}

const PROVIDER: Record<string, string> = { eks: 'Amazon EKS', aws: 'Amazon EKS', k3s: '온프렘 K3s', rke2: '온프렘 RKE2', onprem: '온프렘', gke: 'GKE', gcp: 'GKE' };

function envRows(envs: EnvSummary[] | undefined, clusters: ClustersResp | undefined): EnvRow[] {
  const rows: EnvRow[] = [];
  const used = new Set<string>();
  const findCluster = (e: EnvSummary) =>
    clusters?.clusters.find((c) => [e.id, e.context, e.name].includes(c.cluster_id) || (c.context !== null && [e.id, e.context, e.name].includes(c.context)));
  for (const e of envs ?? []) {
    const c = findCluster(e);
    const nc = clusters?.not_configured.find((n) => [e.id, e.context, e.name].includes(n.cluster_id));
    if (c) used.add(c.cluster_id);
    if (nc) used.add(nc.cluster_id);
    rows.push({
      key: `api:${e.id}`,
      name: e.name ?? e.id,
      provider: e.provider,
      apiStatus: e.status,
      inApi: true,
      cluster: c ? (c.status === 'ok' ? 'ok' : 'down') : nc ? 'none' : 'unknown',
      clusterNote: c ? (c.status === 'ok' ? `${c.cluster_id} 응답` : c.error ?? '닿지 않음') : nc ? nc.reason : '수집기 등록부에 이 환경이 없음',
      nodes: c && c.status === 'ok' ? `${c.nodes.filter((n) => n.ready).length} / ${c.nodes.length}` : null,
      apps: e.apps,
    });
  }
  for (const c of clusters?.clusters ?? []) {
    if (used.has(c.cluster_id)) continue;
    rows.push({
      key: `col:${c.cluster_id}`,
      name: c.display_name ?? c.cluster_id,
      provider: c.provider,
      apiStatus: null,
      inApi: false,
      cluster: c.status === 'ok' ? 'ok' : 'down',
      clusterNote: c.status === 'ok' ? `${c.cluster_id} 응답` : c.error ?? '닿지 않음',
      nodes: c.status === 'ok' ? `${c.nodes.filter((n) => n.ready).length} / ${c.nodes.length}` : null,
      apps: [],
    });
  }
  for (const n of clusters?.not_configured ?? []) {
    if (used.has(n.cluster_id)) continue;
    rows.push({ key: `nc:${n.cluster_id}`, name: n.cluster_id, provider: null, apiStatus: null, inApi: false, cluster: 'none', clusterNote: n.reason, nodes: null, apps: [] });
  }
  return rows;
}

const CLUSTER_TAG: Record<EnvRow['cluster'], { v: 'success' | 'error' | 'neutral'; l: string }> = {
  ok: { v: 'success', l: '정상' },
  down: { v: 'error', l: '응답 없음' },
  none: { v: 'neutral', l: '연결 안 됨' },
  unknown: { v: 'neutral', l: '연결 안 됨' },
};

function NewMigration({ envs, token, onCreated }: { envs: EnvSummary[]; token: string; onCreated: (id: string) => void }) {
  const api = useApi();
  const [source, setSource] = useState('');
  const [target, setTarget] = useState('');
  const [app, setApp] = useState('');
  const srcEnv = envs.find((e) => e.id === source);
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
  const options = envs.map((e) => ({ value: e.id, label: `${e.name ?? e.id}${e.provider ? ` · ${PROVIDER[e.provider] ?? e.provider}` : ''}` }));
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
          {srcEnv && srcEnv.apps.length > 0 ? (
            <Selector label="앱" options={srcEnv.apps.map((a) => ({ value: a, label: a }))} value={app} onChange={setApp} placeholder="앱 고르기…" isRequired width={200} />
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
        <p className="foot">계획을 만들면 그 계획 화면으로 갑니다. 게이트 · 승인 · 컷오버는 계획 화면에서 이어집니다. 승인은 다른 사람(승인자)이 합니다.</p>
      </VStack>
    </form>
  );
}

export function HomeScreen({ session, onOpenPlan }: { session: Session; onOpenPlan: (id: string) => void }) {
  const api = useApi();
  const token = session.user?.token ?? '';
  const plans = useQuery({ queryKey: ['plans', token], queryFn: () => api.listPlans(token), refetchInterval: 5000, retry: 0 });
  const envs = useQuery({ queryKey: ['environments', token], queryFn: () => api.listEnvironments(token), refetchInterval: 10_000, retry: 0 });
  const clusters = useLive('collector', 'v1/clusters', 5000, parseClusters);

  const me = session.me.data;
  const role = me?.role ?? session.user?.role ?? null;
  const email = me?.email ?? session.user?.email ?? null;
  const rows = envRows(envs.data, clusters.data);
  const waiting = (plans.data ?? []).filter((p) => p.awaiting);

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
        { key: 's', header: '상태', render: (p) => (p.state ? <Tag variant={ALERT.has(p.state) ? 'error' : p.state === 'WATCH' ? 'success' : 'neutral'} label={p.state} /> : <Val v={null} />) },
        { key: 'st', header: '출발 → 목적', weight: 3, render: (p) => <span>{p.source ?? '—'} → {p.target ?? '—'}</span> },
        { key: 'a', header: '승인 대기', render: (p) => (p.awaiting ? <Tag variant="warning" label={STAGE_TEXT[p.awaiting] ?? p.awaiting} /> : <span className="foot">없음</span>) },
        { key: 'by', header: '만든 사람', weight: 2, render: (p) => <span className="foot">{p.created_by ?? '—'}</span> },
        { key: 't', header: '만든 시각', numeric: true, px: 150, render: (p) => fmtDateTime(p.created_at) },
      ]}
    />
  );

  return (
    <Screen title="홈" lead="환경 상태, 이전 계획, 내가 할 일을 한 화면에서 봅니다. 계획 ID를 몰라도 됩니다. 실시간 화면은 왼쪽 메뉴에서 바로 열립니다.">
      <Block title="로그인">
        {session.me.error && !session.user ? (
          <NotConnected endpoint="GET /v1/me" reason={apiReason(session.me.error)} />
        ) : (
          <Facts
            items={[
              { label: '사용자', value: email ?? <Val v={null} why={session.me.loading ? '확인 중…' : '응답에 이메일 없음'} /> },
              { label: '역할', value: role ? `${ROLE_TEXT[role] ?? role} (${role})` : <Val v={null} why={me?.roleRaw ? `알 수 없는 역할 ${me.roleRaw}` : '역할 없음'} /> },
              { label: '확인 경로', value: <span className="mono">{me ? 'GET /v1/me' : session.user ? '개발용 토큰' : '—'}</span> },
            ]}
          />
        )}
      </Block>

      {role === 'operator' && (
        <Block title="새 이전 시작">
          {envs.error ? (
            <NotConnected endpoint="GET /v1/environments" reason={`환경 목록이 있어야 출발 · 목적을 고를 수 있습니다. ${apiReason(envs.error)}`} />
          ) : !envs.data ? (
            <p className="foot">환경 목록을 불러오는 중…</p>
          ) : envs.data.length < 2 ? (
            <p className="foot">등록된 환경이 {envs.data.length}개라 이전을 만들 수 없습니다. 출발과 목적 환경이 모두 등록되어야 합니다.</p>
          ) : (
            <NewMigration envs={envs.data} token={token} onCreated={onOpenPlan} />
          )}
        </Block>
      )}

      {role === 'approver' && (
        <Block title={`내 승인 대기 (${waiting.length}건)`}>
          {plans.error ? (
            <NotConnected endpoint="GET /v1/plans" reason={apiReason(plans.error)} />
          ) : (
            planTable(waiting, '지금 승인을 기다리는 계획이 없습니다.')
          )}
          <p className="foot">승인은 계획 화면의 승인 칸에서 합니다. 계획을 만든 사람이나 승인을 요청한 사람은 승인할 수 없습니다(직무 분리).</p>
        </Block>
      )}

      <Block title="환경" meta={<Fresh at={Math.max(envs.dataUpdatedAt, clusters.updatedAt)} />}>
        {envs.error ? <NotConnected title="API 환경 목록 연결 안 됨" endpoint="GET /v1/environments" reason={apiReason(envs.error)} /> : null}
        {clusters.error ? <NotConnected title="클러스터 상태 연결 안 됨" endpoint={clusters.endpoint} reason={`수집기에 닿지 못했습니다. ${liveReason(clusters.error)}`} /> : null}
        <DataTable
          rows={rows}
          rowKey={(r) => r.key}
          empty={envs.isLoading || clusters.isLoading ? '불러오는 중…' : '보여 줄 환경이 없습니다(API 목록과 수집기 모두 비어 있음).'}
          cols={[
            { key: 'n', header: '환경', weight: 2, render: (r) => <VStack gap={0}><span>{r.name}</span><span className="foot">{r.inApi ? 'API 등록' : '수집기에만 있음'}</span></VStack> },
            { key: 'p', header: '종류', render: (r) => (r.provider ? PROVIDER[r.provider] ?? r.provider : <Val v={null} />) },
            { key: 'c', header: '클러스터', weight: 3, render: (r) => <VStack gap={0}><span><Tag variant={CLUSTER_TAG[r.cluster].v} label={CLUSTER_TAG[r.cluster].l} /></span>{r.cluster !== 'ok' && <span className="foot">{r.clusterNote}</span>}</VStack> },
            { key: 'nodes', header: '노드 Ready', numeric: true, render: (r) => <Val v={r.nodes} /> },
            { key: 'apps', header: '앱', weight: 2, render: (r) => (r.apps.length ? <span className="mono">{r.apps.join(', ')}</span> : <span className="foot">{r.inApi ? '응답에 앱 목록 없음' : '—'}</span>) },
            { key: 'st', header: 'API 상태', render: (r) => (r.apiStatus ? <span className="mono">{r.apiStatus}</span> : <Val v={null} />) },
          ]}
        />
      </Block>

      <Block title={`이전 계획${plans.data ? ` (${plans.data.length}건)` : ''}`} meta={<Fresh at={plans.dataUpdatedAt} />}>
        {plans.error ? <NotConnected endpoint="GET /v1/plans" reason={apiReason(plans.error)} /> : planTable(plans.data ?? [], plans.isLoading ? '불러오는 중…' : '아직 계획이 없습니다.')}
      </Block>
    </Screen>
  );
}
