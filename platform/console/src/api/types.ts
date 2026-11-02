/**
 * Types for the pilot API (deploy/pilot/CONTRACT.md §5–§7, platform/api/README.md §8.9).
 *
 * The contract fixes the SSE envelope, state names, event kinds, and the Executor
 * run/stage/receipt columns. Bodies of GET /v1/plans/{id}, POST /gate, POST /approvals and
 * per-kind event `data` are not in the contract yet (due 11/3). They follow the API code
 * as read on 2026-11-02 (platform/api/src/bravo_api/service.py, shift.py, models.py) and are
 * parsed defensively in normalize.ts, so a missing field shows as "값 없음".
 */

/** Spec §8.2 state names (CONTRACT §7). */
export const MAIN_STATES = [
  'COLLECT',
  'ANALYZE',
  'PROPOSE',
  'REHEARSE',
  'GATE',
  'AWAIT_APPROVAL',
  'CUTOVER',
  'WATCH',
] as const;
export const ALERT_STATES = ['SAFE_HOLD', 'ABORT_PRE_WRITE'] as const;

export type MainState = (typeof MAIN_STATES)[number];
export type AlertState = (typeof ALERT_STATES)[number];
export type PlanState = MainState | AlertState;

export function isPlanState(v: unknown): v is PlanState {
  return (
    typeof v === 'string' &&
    ((MAIN_STATES as readonly string[]).includes(v) || (ALERT_STATES as readonly string[]).includes(v))
  );
}

export type Role = 'operator' | 'approver' | 'viewer';
export const ROLES: readonly Role[] = ['operator', 'approver', 'viewer'];

/**
 * `mode` on events: `live` or `recorded` only. `unknown` is local: the server sent another value,
 * and the screen says so instead of guessing.
 */
export type EventMode = 'live' | 'recorded' | 'unknown';

/** States the pilot does not enter (team lead decision 11/2): shown as out of scope. */
export const OUT_OF_SCOPE_STATES: readonly PlanState[] = ['ANALYZE', 'PROPOSE', 'REHEARSE'];
export type ActorKind = 'console' | 'mcp' | 'system';

export type EventKind =
  | 'state.changed'
  | 'gate.verdict'
  | 'approval.granted'
  | 'run.stage'
  | 'run.done'
  | 'receipt.saved';

/** SSE envelope (CONTRACT §7, fixed). */
export interface PlanEvent {
  id: string;
  plan_id: string;
  state: PlanState;
  kind: EventKind | (string & {});
  at: string;
  mode: EventMode;
  run_id: string | null;
  actor: { kind: ActorKind; principal: string | null };
  data: Record<string, unknown>;
}

/** Executor control values (CONTRACT §6). */
export type ControlValue = 'PASS' | 'FAIL' | 'UNKNOWN';
export type Verdict = 'PASS' | 'BLOCK';

export interface ControlRow {
  control_id: string;
  question: string;
  src: ControlValue;
  dst: ControlValue;
  verdict: Verdict;
  /** true when the API sent no per-row verdict and the console applied 04-governance §2. */
  verdictDerived: boolean;
  /** Server reason code for a BLOCK row, e.g. regression. */
  reason: string | null;
}

/** Readiness check shown under the controls (복제 지연, 스키마 해시). */
export interface ReadinessRow {
  check_id: string;
  label: string;
  src: string | null;
  dst: string | null;
  /** null when the API did not send a verdict for this row. */
  verdict: Verdict | null;
  note: string | null;
}

/** POST /v1/plans/{id}/gate body, plan_get `last_gate`, and `gate.verdict` event data. */
export interface GateResult {
  verdict_id: string;
  verdict: Verdict;
  decided_at: string | null;
  revision: string | null;
  /** Server reason codes, e.g. DB-ANON-READ:regression, readiness:replication_lag. */
  reasons: string[];
  controls: ControlRow[];
  readiness: ReadinessRow[];
}

/** run_stages row (CONTRACT §5). */
export interface RunStage {
  seq: number;
  name: string;
  started_at: string | null;
  ms: number | null;
  result: string;
}

export type RunStatus = 'RUNNING' | 'DONE' | 'ABORT_PRE_WRITE' | 'SAFE_HOLD';

export interface HashPair {
  src: string | null;
  dst: string | null;
}

/** receipts row; `body` fields follow CONTRACT §6 step 5 (rows, PK set hash, content hash, sequences). */
export interface Receipt {
  receipt_id: string | null;
  verdict: 'MATCH' | 'MISMATCH' | (string & {});
  created_at: string | null;
  rows: HashPair;
  pk_hash: HashPair;
  content_hash: HashPair;
  seq: HashPair;
}

/** Executor GET /v1/runs/{id} shape (CONTRACT §6) plus run_id. */
export interface Run {
  run_id: string;
  status: RunStatus | (string & {});
  write_pause_ms: number | null;
  write_pause_budget_ms: number | null;
  stages: RunStage[];
  receipt: Receipt | null;
}

/** plan_get `awaiting_approval`: present while AWAIT_APPROVAL has no decision yet. */
export interface ApprovalRequest {
  approval_request_id: string;
  stage: string;
  revision: string;
  gate_verdict_id: string | null;
  expires_at: string | null;
}

/** plan_get `approval` merged with `approval.granted` event data. */
export interface Approval {
  approval_id: string;
  approval_request_id: string | null;
  stage: string;
  revision: string | null;
  gate_verdict_id: string | null;
  expires_at: string | null;
  approver: string | null;
  granted_at: string | null;
  /** true once a shift used this approval (plan_get `approval.consumed`). */
  consumed: boolean | null;
}

export interface DependencyCounts {
  config: number | null;
  adapter: number | null;
  code: number | null;
}

/** GET /v1/plans/{id} (service.py `_plan_view`). */
export interface Plan {
  plan_id: string;
  state: PlanState;
  state_reason: string | null;
  revision: string | null;
  workload: string | null;
  source: string | null;
  target: string | null;
  created_by: string | null;
  last_gate: GateResult | null;
  awaiting_approval: ApprovalRequest | null;
  approval: Approval | null;
  run: Run | null;
  dependencies: DependencyCounts | null;
}

/** POST /v1/plans body (platform/api models.PlanCreate, extra fields rejected). */
export interface CreatePlanInput {
  workload: string;
  source_environment_id: string;
  target_environment_id: string;
}

export interface DevToken {
  role: Role;
  email: string;
  token: string;
}

/** POST /v1/plans/{id}/approvals body (models.ApprovalDecision). The approver is never sent. */
export interface ApprovalInput {
  stage: 'cutover';
  approval_request_id: string;
  revision: number;
  gate_verdict_id: string | null;
}
