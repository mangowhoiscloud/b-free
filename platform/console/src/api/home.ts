/**
 * Home-screen reads: who is logged in, the plan list and the environment list.
 * GET /v1/me, GET /v1/plans and GET /v1/environments are being added by the platform-API
 * thread; their final shapes are not in a contract yet, so parsing is defensive and a missing
 * field shows as "—", never a guessed value.
 */
import { asObj, asStr } from './normalize';
import type { Role } from './types';

export interface Me {
  email: string | null;
  role: Role | null;
  roleRaw: string | null;
  tenant: string | null;
}

export interface PlanSummary {
  plan_id: string;
  state: string | null;
  /** Why the plan holds in its state (e.g. collector_unconfigured in ANALYZE, audit A4). */
  stateReason: string | null;
  workload: string | null;
  source: string | null;
  target: string | null;
  created_at: string | null;
  created_by: string | null;
  /** Stage waiting for approval (change / cutover / failback), or null. */
  awaiting: string | null;
}

export interface EnvSummary {
  id: string;
  name: string | null;
  /** source / target (API `role`). */
  role: string | null;
  provider: string | null;
  status: string | null;
  context: string | null;
  apps: string[];
  /** API's own join with the collector (`cluster`): ok / unreachable / not_configured / unknown. */
  clusterStatus: string | null;
  clusterReason: string | null;
  clusterNodes: number | null;
  /** Secret reference check (`secret_status`); the reference itself is not shown. */
  secretStatus: string | null;
}

const ROLES: readonly string[] = ['operator', 'approver', 'viewer'];
const list = (body: unknown, ...keys: string[]): unknown[] => {
  if (Array.isArray(body)) return body;
  const o = asObj(body);
  for (const k of keys) if (o && Array.isArray(o[k])) return o[k] as unknown[];
  return [];
};
const strs = (v: unknown): string[] => (Array.isArray(v) ? v.map((x) => asStr(x) ?? asStr(asObj(x)?.name) ?? asStr(asObj(x)?.workload)).filter((x): x is string => !!x) : []);
const nameOf = (v: unknown): string | null => asStr(v) ?? asStr(asObj(v)?.display_name) ?? asStr(asObj(v)?.name) ?? asStr(asObj(v)?.environment_id);

export function parseMe(body: unknown): Me {
  const o = asObj(body) ?? {};
  const p = asObj(o.principal) ?? {};
  const rolesArr = Array.isArray(o.roles) ? o.roles : [];
  const roleRaw = asStr(o.role) ?? asStr(p.role) ?? asStr(rolesArr[0]) ?? null;
  return {
    email: asStr(o.email) ?? asStr(p.email) ?? null,
    // security_approver approves too; the UI treats it as approver and shows the raw role.
    role: roleRaw === 'security_approver' ? 'approver' : roleRaw && ROLES.includes(roleRaw) ? (roleRaw as Role) : null,
    roleRaw,
    tenant: asStr(o.tenant) ?? asStr(o.tenant_id) ?? asStr(p.tenant_id) ?? null,
  };
}

export function parsePlans(body: unknown): PlanSummary[] {
  return list(body, 'plans', 'items')
    .map((x) => {
      const o = asObj(x) ?? {};
      const id = asStr(o.plan_id) ?? asStr(o.id);
      if (!id) return null;
      const aw = o.pending_approval ?? o.awaiting_approval;
      const awaiting = asStr(asObj(aw)?.stage) ?? asStr(aw) ?? null;
      return {
        plan_id: id,
        state: asStr(o.state),
        stateReason: asStr(o.state_reason),
        workload: asStr(o.workload),
        source: nameOf(o.source_name) ?? nameOf(o.source) ?? asStr(o.source_environment_id),
        target: nameOf(o.target_name) ?? nameOf(o.target) ?? asStr(o.target_environment_id),
        created_at: asStr(o.created_at),
        created_by: asStr(o.created_by) ?? asStr(asObj(o.created_by)?.email),
        awaiting,
      } satisfies PlanSummary;
    })
    .filter((x): x is PlanSummary => x !== null);
}

export function parseEnvironments(body: unknown): EnvSummary[] {
  return list(body, 'environments', 'items')
    .map((x) => {
      const o = asObj(x) ?? {};
      const id = asStr(o.environment_id) ?? asStr(o.id);
      if (!id) return null;
      const c = asObj(o.cluster) ?? {};
      const nodes = c.nodes;
      return {
        id,
        name: asStr(o.display_name) ?? asStr(o.name),
        role: asStr(o.role),
        provider: asStr(c.provider) ?? asStr(o.provider) ?? asStr(o.kind),
        status: asStr(o.status) ?? asStr(o.state),
        context: asStr(c.context) ?? asStr(o.context),
        apps: strs(o.apps ?? o.workloads),
        clusterStatus: asStr(c.status),
        clusterReason: asStr(c.error) ?? asStr(c.reason),
        clusterNodes: typeof nodes === 'number' ? nodes : null,
        secretStatus: asStr(o.secret_status),
      } satisfies EnvSummary;
    })
    .filter((x): x is EnvSummary => x !== null);
}
