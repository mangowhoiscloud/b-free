/**
 * Defensive parsers from untyped JSON to the console types.
 * Missing values become null so the screen shows "값 없음" instead of inventing data.
 */
import {
  ROLES,
  isPlanState,
  type Approval,
  type ApprovalRequest,
  type ControlRow,
  type ControlValue,
  type DependencyCounts,
  type DevToken,
  type EventMode,
  type ActorKind,
  type GateResult,
  type HashPair,
  type AppliedException,
  type ControlException,
  type Plan,
  type PlanAnalysis,
  type PlanEvent,
  type ReadinessRow,
  type Receipt,
  type Role,
  type Run,
  type RunStage,
  type Verdict,
} from './types';

type Json = Record<string, unknown>;

export function asObj(v: unknown): Json | null {
  return typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Json) : null;
}
export function asStr(v: unknown): string | null {
  if (typeof v === 'string') return v;
  if (typeof v === 'number' && Number.isFinite(v)) return String(v);
  return null;
}
export function asNum(v: unknown): number | null {
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  if (typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v))) return Number(v);
  return null;
}
function asArr(v: unknown): unknown[] {
  return Array.isArray(v) ? v : [];
}

function controlValue(v: unknown): ControlValue {
  return v === 'PASS' || v === 'FAIL' ? v : 'UNKNOWN';
}
function verdict(v: unknown): Verdict | null {
  if (v === 'PASS' || v === 'BLOCK') return v;
  return null;
}

/** 04-governance.md §2: only PASS/PASS passes; FAIL or UNKNOWN on either side blocks. */
export function ruleVerdict(src: ControlValue, dst: ControlValue): Verdict {
  return src === 'PASS' && dst === 'PASS' ? 'PASS' : 'BLOCK';
}

/** Pilot control questions, CONTRACT §6. Used when a row carries no `question`. */
const CONTROL_QUESTION: Record<string, string> = {
  'DB-ANON-READ': 'PUBLIC이 messages를 읽을 수 있는가 (읽을 수 없으면 PASS)',
  'DB-AUTH': 'app 역할이 비밀번호 없이 접속할 수 있는가 (불가면 PASS)',
};

function controlRow(v: unknown): ControlRow | null {
  const o = asObj(v);
  const id = asStr(o?.control_id);
  if (!o || !id) return null;
  const src = controlValue(o.src);
  const dst = controlValue(o.dst);
  const sentRaw = o.verdict === 'EXCEPTION' ? 'EXCEPTION' : verdict(o.verdict);
  return {
    control_id: id,
    question: asStr(o.question) ?? CONTROL_QUESTION[id] ?? '',
    src,
    dst,
    verdict: sentRaw ?? ruleVerdict(src, dst),
    verdictDerived: sentRaw === null,
    reason: asStr(o.reason),
    evidence_digest: asStr(o.evidence_digest),
    exception_id: asStr(o.exception_id),
  };
}

function pairStr(v: unknown): HashPair {
  const o = asObj(v);
  return { src: asStr(o?.src), dst: asStr(o?.dst) };
}

/**
 * Readiness rows. The API (gate.py) judges readiness and reports failures only as
 * `reasons` (readiness:replication_lag, readiness:lag_unknown, readiness:schema_mismatch,
 * readiness:schema_hash_unknown). Values come from the POST /gate response
 * `readiness: {lag_bytes, schema_match}` when present. No reason on a judged gate means passed.
 */
function readinessFromReasons(reasons: string[], values: unknown, sentVerdict: Verdict | null): ReadinessRow[] {
  const o = asObj(values);
  const lag = asNum(o?.lag_bytes);
  const match = typeof o?.schema_match === 'boolean' ? o.schema_match : null;
  const has = (r: string) => reasons.includes(`readiness:${r}`);
  const judged = sentVerdict !== null;
  const lagBlock = has('replication_lag') || has('lag_unknown');
  const schemaBlock = has('schema_mismatch') || has('schema_hash_unknown');
  return [
    {
      check_id: 'REPL-LAG',
      label: readinessLabel('REPL-LAG'),
      src: null,
      dst: lag === null || has('lag_unknown') ? null : `${lag.toLocaleString('ko-KR')} B`,
      verdict: lagBlock ? 'BLOCK' : judged ? 'PASS' : null,
      note: has('lag_unknown') ? '지연 값을 읽지 못함' : has('replication_lag') ? '기준보다 큼' : '목적지 구독의 복제 지연',
    },
    {
      check_id: 'SCHEMA-HASH',
      label: readinessLabel('SCHEMA-HASH'),
      src: null,
      // The API reports schema_match=false when a hash is missing; the reason code tells which.
      dst: match === null || has('schema_hash_unknown') ? null : match ? '원본과 같음' : '원본과 다름',
      verdict: schemaBlock ? 'BLOCK' : judged ? 'PASS' : null,
      note: has('schema_hash_unknown') ? '해시를 읽지 못함' : has('schema_mismatch') ? '원본과 다름' : '원본 · 대상 스키마 해시 비교',
    },
  ];
}

function readiness(v: unknown): ReadinessRow[] {
  if (Array.isArray(v)) {
    return v.flatMap((r): ReadinessRow[] => {
      const o = asObj(r);
      const id = asStr(o?.check_id);
      if (!o || !id) return [];
      return [
        {
          check_id: id,
          label: asStr(o.label) ?? readinessLabel(id),
          src: asStr(o.src),
          dst: asStr(o.dst),
          verdict: verdict(o.verdict),
          note: asStr(o.note) ?? asStr(o.reason),
        },
      ];
    });
  }
  const o = asObj(v);
  if (!o) return [];
  const rows: ReadinessRow[] = [];
  const verdicts = asObj(o.verdicts);
  const lag = asNum(o.lag_bytes);
  if (lag !== null || o.sub_state !== undefined) {
    rows.push({
      check_id: 'REPL-LAG',
      label: readinessLabel('REPL-LAG'),
      src: null,
      dst: lag === null ? null : `${lag.toLocaleString('ko-KR')} B`,
      verdict: verdict(verdicts?.lag),
      note: asStr(o.sub_state) ? `구독 상태 ${asStr(o.sub_state)}` : null,
    });
  }
  if (o.schema_hash !== undefined) {
    const p = pairStr(o.schema_hash);
    rows.push({
      check_id: 'SCHEMA-HASH',
      label: readinessLabel('SCHEMA-HASH'),
      src: p.src,
      dst: p.dst,
      verdict: verdict(verdicts?.schema_hash),
      note: p.src !== null && p.dst !== null ? (p.src === p.dst ? '두 값이 같음' : '두 값이 다름') : null,
    });
  }
  return rows;
}

export function readinessLabel(id: string): string {
  switch (id) {
    case 'REPL-LAG':
      return '복제 지연';
    case 'SCHEMA-HASH':
      return '스키마 해시';
    default:
      return id;
  }
}

export function parseGate(v: unknown): GateResult | null {
  const o = asObj(v);
  if (!o) return null;
  const id = asStr(o.verdict_id) ?? asStr(o.gate_verdict_id) ?? asStr(o.id);
  const controls = asArr(o.controls)
    .map(controlRow)
    .filter((r): r is ControlRow => r !== null);
  const reasons = asArr(o.reasons)
    .map(asStr)
    .filter((r): r is string => r !== null);
  const sent = verdict(o.verdict);
  const rows = Array.isArray(o.readiness) ? readiness(o.readiness) : readinessFromReasons(reasons, o.readiness, sent);
  if (!id && controls.length === 0) return null;
  // EXCEPTION rows are out of parity (contract 15 §9.4): only BLOCK rows block.
  const derived: Verdict =
    controls.every((c) => c.verdict !== 'BLOCK') && rows.every((r) => r.verdict !== 'BLOCK') ? 'PASS' : 'BLOCK';
  return {
    verdict_id: id ?? '(ID 없음)',
    verdict: sent ?? derived,
    decided_at: asStr(o.decided_at) ?? asStr(o.at),
    revision: asStr(o.revision) ?? asStr(o.plan_revision),
    reasons,
    controls,
    readiness: rows,
    exceptions: asArr(o.exceptions)
      .map((x) => {
        const e = asObj(x);
        const eid = asStr(e?.exception_id);
        if (!e || !eid) return null;
        return { exception_id: eid, control_id: asStr(e.control_id) ?? '—', src: asStr(e.src), dst: asStr(e.dst), evidence_digest: asStr(e.evidence_digest), expires_at: asStr(e.expires_at), approved_by: asStr(e.approved_by) };
      })
      .filter((x): x is AppliedException => x !== null),
  };
}

/** Control exception object (contract 15 §9.3). Unknown fields stay null. */
export function parseException(v: unknown): ControlException | null {
  const o = asObj(v);
  const id = asStr(o?.exception_id);
  if (!o || !id) return null;
  const env = asObj(o.target_environment);
  return {
    exception_id: id,
    control_id: asStr(o.control_id) ?? '—',
    target_environment: asStr(env?.name) ?? asStr(env?.environment_id),
    gate_verdict_id: asStr(o.gate_verdict_id),
    src: asStr(o.src),
    dst: asStr(o.dst),
    evidence_digest: asStr(o.evidence_digest),
    reason: asStr(o.reason),
    expires_at: asStr(o.expires_at),
    status: asStr(o.status) ?? 'requested',
    stale_reason: asStr(o.stale_reason),
    requested_by: asStr(o.requested_by),
    requested_at: asStr(o.requested_at),
    decided_by: asStr(o.decided_by),
    decided_at: asStr(o.decided_at),
    decision_note: asStr(o.decision_note),
    applied: o.applied === true,
  };
}

function stage(v: unknown): RunStage | null {
  const o = asObj(v);
  const seq = asNum(o?.seq);
  if (!o || seq === null) return null;
  return {
    seq,
    name: asStr(o.name) ?? '',
    started_at: asStr(o.started_at),
    ms: asNum(o.ms),
    result: asStr(o.result) ?? '',
  };
}

export function parseStage(v: unknown): RunStage | null {
  return stage(v);
}

export function parseReceipt(v: unknown): Receipt | null {
  const o = asObj(v);
  if (!o) return null;
  // Accept either a receipts row {receipt_id, verdict, body:{...}} or a flat body.
  const body = asObj(o.body) ?? o;
  const verdictValue = asStr(o.verdict) ?? asStr(body.verdict);
  if (!verdictValue) return null;
  const parts = asObj(body.verdict_parts) ?? {};
  const objects = asObj(body.objects) ?? {};
  const numMap = (x: unknown) => Object.fromEntries(Object.entries(asObj(x) ?? {}).map(([k, n]) => [k, typeof n === 'number' ? n : null]));
  const strMap = (x: unknown) => Object.fromEntries(Object.entries(asObj(x) ?? {}).map(([k, n]) => [k, asStr(n)]));
  return {
    receipt_id: asStr(o.receipt_id) ?? asStr(body.receipt_id),
    verdict: verdictValue,
    created_at: asStr(o.created_at) ?? asStr(body.created_at),
    source: asStr(body.source),
    target: asStr(body.target),
    direction: asStr(body.direction),
    parts: { tables: asStr(parts.tables), sequences: asStr(parts.sequences), objects: asStr(parts.objects) },
    objects: { verdict: asStr(objects.verdict), reason: asStr(objects.reason), referenced: typeof objects.referenced === 'number' ? objects.referenced : null },
    tables: (Array.isArray(body.tables) ? body.tables : []).map((t) => {
      const x = asObj(t) ?? {};
      return { name: asStr(x.name) ?? '—', rows: numMap(x.rows), pkHash: strMap(x.pk_hash), contentHash: strMap(x.content_hash), match: typeof x.match === 'boolean' ? x.match : null };
    }),
    sequences: (Array.isArray(body.sequences) ? body.sequences : []).map((t) => {
      const x = asObj(t) ?? {};
      return { name: asStr(x.name) ?? '—', originLast: asNum(x.origin_last), targetBefore: asNum(x.target_before), targetSet: asNum(x.target_set), match: typeof x.match === 'boolean' ? x.match : null };
    }),
    rows: pairStr(body.rows),
    pk_hash: pairStr(body.pk_hash),
    content_hash: pairStr(body.content_hash),
    seq: pairStr(Array.isArray(body.sequences) ? null : body.seq ?? body.sequences),
  };
}

export function parseRun(v: unknown): Run | null {
  const o = asObj(v);
  const id = asStr(o?.run_id);
  if (!o || !id) return null;
  return {
    run_id: id,
    status: asStr(o.status) ?? 'RUNNING',
    write_pause_ms: asNum(o.write_pause_ms),
    write_pause_budget_ms: asNum(o.write_pause_budget_ms),
    stages: asArr(o.stages)
      .map(stage)
      .filter((s): s is RunStage => s !== null)
      .sort((a, b) => a.seq - b.seq),
    receipt: parseReceipt(o.receipt),
  };
}

function approvalRequest(v: unknown): ApprovalRequest | null {
  const o = asObj(v);
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

export function parseApproval(v: unknown): Approval | null {
  const o = asObj(v);
  const id = asStr(o?.approval_id);
  if (!o || !id) return null;
  return {
    approval_id: id,
    approval_request_id: asStr(o.approval_request_id),
    stage: asStr(o.stage) ?? 'cutover',
    revision: asStr(o.revision) ?? asStr(o.plan_revision),
    gate_verdict_id: asStr(o.gate_verdict_id),
    expires_at: asStr(o.expires_at),
    approver: asStr(o.approver) ?? asStr(asObj(o.approver)?.email),
    granted_at: asStr(o.granted_at) ?? asStr(o.at),
    consumed: typeof o.consumed === 'boolean' ? o.consumed : null,
  };
}

function dependencies(v: unknown): DependencyCounts | null {
  const o = asObj(v);
  if (!o) return null;
  const c: DependencyCounts = {
    config: asNum(o.config ?? o.config_swap),
    adapter: asNum(o.adapter ?? o.adapter_add),
    code: asNum(o.code ?? o.code_change),
  };
  return c.config === null && c.adapter === null && c.code === null ? null : c;
}

export function parsePlan(v: unknown): Plan {
  const o = asObj(v) ?? {};
  const state = isPlanState(o.state) ? o.state : 'COLLECT';
  let run = parseRun(o.run ?? o.active_run);
  // plan_get returns the last receipt beside the run (service.py `last_receipt`).
  const receipt = parseReceipt(o.last_receipt);
  const receiptRun = asStr(asObj(o.last_receipt)?.run_id);
  if (run && receipt && !run.receipt && (receiptRun === null || receiptRun === run.run_id)) run = { ...run, receipt };
  return {
    plan_id: asStr(o.plan_id) ?? asStr(o.id) ?? '',
    state,
    state_reason: asStr(o.state_reason),
    revision: asStr(o.revision),
    workload: asStr(o.workload),
    source: asStr(o.source_environment_id) ?? asStr(o.source),
    target: asStr(o.target_environment_id) ?? asStr(o.target),
    // plan_get sends {created_by: {principal: <email>}} (service.py).
    created_by: asStr(o.created_by) ?? asStr(asObj(o.created_by)?.principal) ?? asStr(asObj(o.created_by)?.email),
    last_gate: parseGate(o.last_gate ?? o.gate),
    awaiting_approval: approvalRequest(o.awaiting_approval),
    approval: parseApproval(o.approval),
    run,
    dependencies: dependencies(o.dependencies ?? o.dependency_counts),
    analysis: parseAnalysis(o.analysis),
  };
}

function parseAnalysis(v: unknown): PlanAnalysis | null {
  const a = asObj(v);
  if (!a) return null;
  const inv = asObj(a.inventory) ?? {};
  const grades = asObj(inv.by_grade) ?? {};
  return {
    inventoryStatus: asStr(inv.status),
    inventoryReason: asStr(inv.reason),
    filesScanned: typeof inv.files_scanned === 'number' ? inv.files_scanned : null,
    dependencies: typeof inv.dependencies === 'number' ? inv.dependencies : null,
    byGrade: Object.fromEntries(Object.entries(grades).filter(([, n]) => typeof n === 'number')) as Record<string, number>,
    gaps: Array.isArray(a.gaps) ? a.gaps.length : 0,
    controlDifferences: Array.isArray(a.control_differences) ? a.control_differences.filter((x): x is string => typeof x === 'string') : [],
    analyzedAt: asStr(a.analyzed_at),
  };
}

function mode(v: unknown): EventMode {
  return v === 'live' || v === 'recorded' ? v : 'unknown';
}
function actorKind(v: unknown): ActorKind {
  return v === 'console' || v === 'mcp' ? v : 'system';
}

export function parseEvent(raw: string, fallbackId: string): PlanEvent | null {
  let v: unknown;
  try {
    v = JSON.parse(raw);
  } catch {
    return null;
  }
  const o = asObj(v);
  if (!o) return null;
  const state = o.state;
  if (!isPlanState(state)) return null;
  const actor = asObj(o.actor);
  return {
    id: asStr(o.id) ?? fallbackId,
    plan_id: asStr(o.plan_id) ?? '',
    state,
    kind: asStr(o.kind) ?? 'unknown',
    at: asStr(o.at) ?? new Date().toISOString(),
    mode: mode(o.mode),
    run_id: asStr(o.run_id),
    actor: { kind: actorKind(actor?.kind), principal: asStr(actor?.principal) },
    data: asObj(o.data) ?? {},
  };
}

/** Reads the `email` claim without verifying. A UI label only; the API verifies every token. */
export function emailFromJwt(token: string): string {
  const part = token.split('.')[1];
  if (part) {
    try {
      const padded = part.replace(/-/g, '+').replace(/_/g, '/');
      const bytes = Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
      const claims = asObj(JSON.parse(new TextDecoder().decode(bytes)));
      const email = asStr(claims?.email) ?? asStr(claims?.sub);
      if (email) return email;
    } catch {
      /* not a JWT: fall through */
    }
  }
  return '이메일 없음';
}

function isRole(v: unknown): v is Role {
  return typeof v === 'string' && (ROLES as readonly string[]).includes(v);
}

/** GET /v1/dev/tokens: accepts [{role,email,token}] or {operator:"jwt"|{email,token},...}. */
export function parseDevTokens(v: unknown): DevToken[] {
  const out: DevToken[] = [];
  if (Array.isArray(v)) {
    for (const item of v) {
      const o = asObj(item);
      const token = asStr(o?.token) ?? asStr(o?.jwt);
      if (o && isRole(o.role) && token) {
        out.push({ role: o.role, email: asStr(o.email) ?? emailFromJwt(token), token });
      }
    }
  } else {
    const o = asObj(v);
    const map = asObj(o?.tokens) ?? o;
    if (map) {
      for (const role of ROLES) {
        const entry = map[role];
        const token = asStr(entry) ?? asStr(asObj(entry)?.token) ?? asStr(asObj(entry)?.jwt);
        if (token) out.push({ role, email: asStr(asObj(entry)?.email) ?? emailFromJwt(token), token });
      }
    }
  }
  return out.sort((a, b) => ROLES.indexOf(a.role) - ROLES.indexOf(b.role));
}
