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
  'SHIFT',
  'CUTOVER',
  'WATCH',
  'DONE',
] as const;
export const ALERT_STATES = ['SAFE_HOLD', 'ABORT_PRE_WRITE'] as const;
/** States off the main line (API statemachine.py): failback after WATCH, and a person needed after rehearsals. */
export const SIDE_STATES = ['FAILBACK', 'DONE_ROLLED_BACK', 'NEEDS_HUMAN'] as const;

export type MainState = (typeof MAIN_STATES)[number];
export type AlertState = (typeof ALERT_STATES)[number];
export type SideState = (typeof SIDE_STATES)[number];
export type PlanState = MainState | AlertState | SideState;

export function isPlanState(v: unknown): v is PlanState {
  return (
    typeof v === 'string' &&
    ((MAIN_STATES as readonly string[]).includes(v) || (ALERT_STATES as readonly string[]).includes(v) || (SIDE_STATES as readonly string[]).includes(v))
  );
}

export type Role = 'operator' | 'approver' | 'viewer';
export const ROLES: readonly Role[] = ['operator', 'approver', 'viewer'];

/**
 * `mode` on events: `live` or `recorded` only. `unknown` is local: the server sent another value,
 * and the screen says so instead of guessing.
 */
export type EventMode = 'live' | 'recorded' | 'unknown';

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
/** Per-row verdict: EXCEPTION = an approved control exception took the row out of parity (contract 15 §9.4). */
export type RowVerdict = Verdict | 'EXCEPTION';

export interface ControlRow {
  control_id: string;
  question: string;
  src: ControlValue;
  dst: ControlValue;
  verdict: RowVerdict;
  /** true when the API sent no per-row verdict and the console applied 04-governance §2. */
  verdictDerived: boolean;
  /** Server reason code for a BLOCK row, e.g. regression; `exception_approved` for EXCEPTION. */
  reason: string | null;
  /** sha256 of the destination evidence; null when the row has no evidence (e.g. Executor rows). */
  evidence_digest: string | null;
  /** Set only on EXCEPTION rows. */
  exception_id: string | null;
  /** Both sources' values when Executor and gate reported the same control (contract 15 §9.5); else null. */
  sources: { source: string; src: string | null; dst: string | null }[] | null;
  /** Sources that reported UNKNOWN or no value while the other measured. */
  missing_sources: string[];
}

/** Exception applied in a gate verdict (`exceptions[]`, contract 15 §9.4). */
export interface AppliedException {
  exception_id: string;
  control_id: string;
  src: string | null;
  dst: string | null;
  evidence_digest: string | null;
  expires_at: string | null;
  approved_by: string | null;
  /** 대체 통제 (contract 15 §9, spec §8.5). */
  compensating_control: string | null;
}

export type ExceptionStatus = 'requested' | 'approved' | 'rejected' | 'stale' | 'expired';

/** Control exception object (contract 15 §9.3). */
export interface ControlException {
  exception_id: string;
  control_id: string;
  target_environment: string | null;
  gate_verdict_id: string | null;
  src: string | null;
  dst: string | null;
  evidence_digest: string | null;
  reason: string | null;
  /** 대체 통제: what stands in for the missing control (contract 15 §9.1). */
  compensating_control: string | null;
  expires_at: string | null;
  status: ExceptionStatus | (string & {});
  stale_reason: string | null;
  requested_by: string | null;
  requested_at: string | null;
  decided_by: string | null;
  decided_at: string | null;
  decision_note: string | null;
  applied: boolean;
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
  exceptions: AppliedException[];
  /** End of the gate evidence validity; no exception may be requested or approved after it. Null = unknown. */
  evidence_expires_at: string | null;
}

/** run_stages row (CONTRACT §5). */
export interface RunStage {
  seq: number;
  name: string;
  started_at: string | null;
  ms: number | null;
  result: string;
  /** front.* and failback.8_front_to_src stages (contract 15 §10.3): weights, judgement, revert. */
  front: Record<string, unknown> | null;
}

/** One judged step of the 10/50/100 shift (contract 15 §10.2 `shift.steps[]`). */
export interface ShiftStep {
  weight: number | null;
  healthy: boolean | null;
  target_samples: number | null;
  target_errors: number | null;
  target_error_rate: number | null;
  target_p95_ms: number | null;
  samples: number | null;
  error_rate: number | null;
  p95_ms: number | null;
  by_env: Record<string, number>;
  hold_ms: number | null;
  step_ms: number | null;
  reasons: string[];
}

/** GET /v1/plans/{id} `shift`: the plan's last shift run. */
export interface ShiftSummary {
  run_id: string;
  status: string;
  target_env: string | null;
  current_weight: number | null;
  started_at: string | null;
  ended_at: string | null;
  steps: ShiftStep[];
  reverted: { at_weight: number | null; reasons: string[] } | null;
}

/** Receipt `scope` (contract 15 §11.1) or the API's `verification_scope` for older receipts (§11.4). */
export interface ReceiptScope {
  source: 'receipt' | 'derived' | (string & {});
  hash_rule: string | null;
  tables: string[];
  sequences: string[];
  objects: { checked: string | null; referenced: number | null; reference_set_sha256: string | null } | string | null;
}

/** GET /v1/plans/{id}/runs/{run_id}/replay (contract 15 §12). */
export interface Replay {
  mode: string;
  run_id: string;
  kind: string | null;
  status: string | null;
  recorded_at: string | null;
  ended_at: string | null;
  generated_at: string | null;
  events: (PlanEvent & { offset_ms: number | null })[];
}

export type RunStatus = 'RUNNING' | 'DONE' | 'ABORT_PRE_WRITE' | 'SAFE_HOLD';

export interface HashPair {
  src: string | null;
  dst: string | null;
}

/** One compared table in the Executor receipt (cutover.py): values keyed by side (src / dst). */
export interface ReceiptTable {
  name: string;
  rows: Record<string, number | null>;
  pkHash: Record<string, string | null>;
  contentHash: Record<string, string | null>;
  match: boolean | null;
}

export interface ReceiptSequence {
  name: string;
  originLast: number | null;
  targetBefore: number | null;
  targetSet: number | null;
  match: boolean | null;
}

/** receipts row; `body` from the Executor (cutover.py): tables[], sequences[], objects, verdict_parts.
 *  MATCH_PARTIAL = tables and sequences matched, objects NOT_CHECKED (audit B10). */
export interface Receipt {
  receipt_id: string | null;
  verdict: 'MATCH' | 'MATCH_PARTIAL' | 'MISMATCH' | (string & {});
  created_at: string | null;
  /** Cluster names and direction as the Executor wrote them. */
  source: string | null;
  target: string | null;
  direction: string | null;
  parts: { tables: string | null; sequences: string | null; objects: string | null };
  objects: { verdict: string | null; reason: string | null; referenced: number | null };
  tables: ReceiptTable[];
  sequences: ReceiptSequence[];
  /** What each part checked: receipt `scope`, else the API's `verification_scope` (§11.4). */
  scope: ReceiptScope | null;
  /** Older pilot body (single pairs); kept so older receipts still render. */
  rows: HashPair;
  pk_hash: HashPair;
  content_hash: HashPair;
  seq: HashPair;
}

/** Executor GET /v1/runs/{id} shape (CONTRACT §6) plus run_id. */
export interface Run {
  run_id: string;
  /** shift · cutover · failback (contract 15 §10.2). */
  kind: string | null;
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
  /** plan.analysis (GET /v1/plans/{id}): inventory status and gaps. Null until ANALYZE has run. */
  analysis: PlanAnalysis | null;
  /** Last shift run (contract 15 §10.2), null when none. */
  shift: ShiftSummary | null;
}

/** service.py `_apply_analysis`: inventory {status, reason, …}, gaps[], control_differences[]. */
export interface PlanAnalysis {
  inventoryStatus: string | null;
  inventoryReason: string | null;
  filesScanned: number | null;
  dependencies: number | null;
  byGrade: Record<string, number>;
  gaps: number;
  controlDifferences: string[];
  analyzedAt: string | null;
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
