/**
 * Builds what the screen shows from the plan snapshot (GET /v1/plans/{id}) and the SSE events.
 * The snapshot is the source of truth; events fill gaps and show changes before the refetch lands.
 */
import { asNum, asObj, asStr, parseApproval, parseGate, parseReceipt, parseStage } from './normalize';
import {
  ALERT_STATES,
  type AlertState,
  type Approval,
  type ApprovalRequest,
  type EventMode,
  type GateResult,
  type Plan,
  type PlanEvent,
  type PlanState,
  type Run,
  type RunStage,
} from './types';

export interface ConsoleView {
  state: PlanState;
  /** Latest `mode` seen for each state (from events that carry that state). */
  modes: Partial<Record<PlanState, EventMode>>;
  /** States that appeared in the event stream or were reached per the snapshot. */
  reached: ReadonlySet<PlanState>;
  alert: AlertState | null;
  gate: GateResult | null;
  gateRequestedBy: PlanEvent['actor'] | null;
  awaiting: ApprovalRequest | null;
  approval: Approval | null;
  run: Run | null;
  stateReason: string | null;
  /** Who created the plan (actor of the plan_created event, or the snapshot). */
  createdBy: string | null;
  /** Who requested approval (actor of the gate run that moved the plan to AWAIT_APPROVAL). */
  approvalRequestedBy: string | null;
}

function approvalRequestFromEvent(e: PlanEvent): ApprovalRequest | null {
  const o = asObj(e.data.approval_request) ?? (e.state === 'AWAIT_APPROVAL' ? e.data : null);
  const id = asStr(o?.approval_request_id);
  const revision = asStr(o?.revision);
  if (!o || !id || !revision) return null;
  return {
    approval_request_id: id,
    stage: asStr(o.stage) ?? 'cutover',
    revision,
    gate_verdict_id: asStr(o.gate_verdict_id),
    expires_at: asStr(o.expires_at),
  };
}

function upsertStage(stages: RunStage[], s: RunStage): RunStage[] {
  const next = stages.filter((x) => x.seq !== s.seq);
  next.push(s);
  return next.sort((a, b) => a.seq - b.seq);
}

function emptyRun(runId: string): Run {
  return { run_id: runId, status: 'RUNNING', write_pause_ms: null, write_pause_budget_ms: null, stages: [], receipt: null };
}

/**
 * Applies one event to a plan. deriveView uses it to merge events newer than the snapshot
 * and to fill fields the snapshot leaves out.
 */
export function applyEvent(plan: Plan, e: PlanEvent): Plan {
  const next: Plan = { ...plan, state: e.state };
  switch (e.kind) {
    case 'state.changed': {
      const req = approvalRequestFromEvent(e);
      if (req) {
        next.awaiting_approval = req;
        next.revision = req.revision;
        if (next.approval && next.approval.approval_request_id !== req.approval_request_id) next.approval = null;
      }
      break;
    }
    case 'gate.verdict': {
      const g = parseGate(e.data);
      if (g) next.last_gate = g;
      break;
    }
    case 'approval.granted': {
      const a = parseApproval(e.data);
      if (a) {
        next.approval = { ...a, approver: a.approver ?? e.actor.principal, granted_at: a.granted_at ?? e.at };
        next.awaiting_approval = null;
      }
      break;
    }
    case 'run.stage': {
      const s = parseStage(asObj(e.data.stage) ?? e.data);
      const runId = e.run_id ?? asStr(e.data.run_id);
      if (s && runId) {
        const run = plan.run && plan.run.run_id === runId ? plan.run : emptyRun(runId);
        next.run = { ...run, stages: upsertStage(run.stages, s) };
      }
      break;
    }
    case 'run.done': {
      const runId = e.run_id ?? asStr(e.data.run_id);
      if (runId) {
        const run = plan.run && plan.run.run_id === runId ? plan.run : emptyRun(runId);
        next.run = {
          ...run,
          status: asStr(e.data.status) ?? run.status,
          write_pause_ms: asNum(e.data.write_pause_ms) ?? run.write_pause_ms,
          write_pause_budget_ms: asNum(e.data.write_pause_budget_ms) ?? run.write_pause_budget_ms,
        };
      }
      break;
    }
    case 'receipt.saved': {
      const r = parseReceipt(asObj(e.data.receipt) ?? e.data);
      const runId = e.run_id ?? asStr(e.data.run_id);
      if (r && runId) {
        const run = plan.run && plan.run.run_id === runId ? plan.run : emptyRun(runId);
        next.run = { ...run, receipt: r };
      }
      break;
    }
    default:
      break;
  }
  return next;
}

export function emptyPlan(planId: string): Plan {
  return {
    plan_id: planId,
    state: 'COLLECT',
    revision: null,
    workload: null,
    source: null,
    target: null,
    created_by: null,
    last_gate: null,
    awaiting_approval: null,
    approval: null,
    run: null,
    dependencies: null,
    analysis: null,
    state_reason: null,
  };
}

/**
 * @param snapshot     latest GET /v1/plans/{id}
 * @param events       all events received for the plan, in stream order
 * @param snapshotSeen number of events already received when the snapshot was fetched;
 *                     only events after that index override snapshot fields.
 */
export function deriveView(
  planId: string,
  snapshot: Plan | undefined,
  events: readonly PlanEvent[],
  snapshotSeen: number,
): ConsoleView {
  const modes: Partial<Record<PlanState, EventMode>> = {};
  const reached = new Set<PlanState>();
  let gateRequestedBy: PlanEvent['actor'] | null = null;
  let createdBy: string | null = null;
  let approvalRequestedBy: string | null = null;
  for (const e of events) {
    modes[e.state] = e.mode;
    reached.add(e.state);
    if (e.kind === 'gate.verdict') gateRequestedBy = e.actor;
    if (e.kind === 'state.changed' && e.data.trigger === 'plan_created') createdBy = e.actor.principal;
    if (e.kind === 'state.changed' && e.state === 'AWAIT_APPROVAL' && e.data.from === 'GATE') approvalRequestedBy = e.actor.principal;
  }

  let plan: Plan;
  if (snapshot) {
    plan = snapshot;
    for (const e of events.slice(snapshotSeen)) plan = applyEvent(plan, e);
    // Fill fields the snapshot left out (e.g. an API that does not embed stages).
    const fromEvents = events.reduce(applyEvent, emptyPlan(planId));
    plan = {
      ...plan,
      last_gate: mergeGate(plan.last_gate, fromEvents.last_gate),
      awaiting_approval: plan.awaiting_approval ?? (plan.state === 'AWAIT_APPROVAL' ? fromEvents.awaiting_approval : null),
      approval: mergeApproval(plan.approval, fromEvents.approval),
      run: mergeRun(plan.run, fromEvents.run),
    };
  } else {
    plan = events.reduce(applyEvent, emptyPlan(planId));
  }
  reached.add(plan.state);

  const alert = (ALERT_STATES as readonly string[]).includes(plan.state) ? (plan.state as AlertState) : null;
  return {
    state: plan.state,
    modes,
    reached,
    alert,
    gate: plan.last_gate,
    gateRequestedBy,
    awaiting: plan.awaiting_approval,
    approval: plan.approval,
    run: plan.run,
    stateReason: plan.state_reason,
    createdBy: plan.created_by ?? createdBy,
    approvalRequestedBy,
  };
}

/** Same approval from the snapshot (id, expiry, consumed) and the event (revision, approver). */
function mergeApproval(a: Approval | null, b: Approval | null): Approval | null {
  if (!a) return b;
  if (!b || a.approval_id !== b.approval_id) return a;
  return {
    approval_id: a.approval_id,
    approval_request_id: a.approval_request_id ?? b.approval_request_id,
    stage: a.stage,
    revision: a.revision ?? b.revision,
    gate_verdict_id: a.gate_verdict_id ?? b.gate_verdict_id,
    expires_at: a.expires_at ?? b.expires_at,
    approver: a.approver ?? b.approver,
    granted_at: a.granted_at ?? b.granted_at,
    consumed: a.consumed ?? b.consumed,
  };
}

/** The snapshot's last_gate lacks readiness values; the event or POST response may have them. */
function mergeGate(a: GateResult | null, b: GateResult | null): GateResult | null {
  if (!a) return b;
  if (!b || a.verdict_id !== b.verdict_id) return a;
  return { ...a, decided_at: a.decided_at ?? b.decided_at, revision: a.revision ?? b.revision };
}

function mergeRun(a: Run | null, b: Run | null): Run | null {
  if (!a) return b;
  if (!b || a.run_id !== b.run_id) return a;
  let stages = a.stages;
  for (const s of b.stages) if (!stages.some((x) => x.seq === s.seq)) stages = upsertStage(stages, s);
  return {
    ...a,
    stages,
    write_pause_ms: a.write_pause_ms ?? b.write_pause_ms,
    write_pause_budget_ms: a.write_pause_budget_ms ?? b.write_pause_budget_ms,
    receipt: a.receipt ?? b.receipt,
  };
}
