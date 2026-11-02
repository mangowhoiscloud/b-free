/** Folds plan SSE events (CONTRACT §7) into agent trials and workflow rows. Pure functions. */
import type { PlanEvent } from '../api/types';
import { asRecord, num, str } from './live';

export const AGENT_ROLES = ['discovery', 'db', 'governance', 'app_migration', 'ci', 'report'] as const;
export type AgentRole = (typeof AGENT_ROLES)[number];

export const ROLE_LABEL: Record<AgentRole, string> = {
  discovery: '발견 discovery',
  db: '데이터베이스 db',
  governance: '거버넌스 governance',
  app_migration: '앱 이전 app_migration',
  ci: 'CI ci',
  report: '보고 report',
};

export interface Denied { seq: number | null; tool: string | null; rule: string | null; at: string | null; eventId: string }

export interface Trial {
  rehearsalId: string;
  trialId: string | null;
  role: string | null;
  attempt: number | null;
  task: string | null;
  phase: 'requested' | 'proposed' | 'verdict';
  verdict: string | null;
  reason: string | null;
  digest: string | null;
  verifier: Record<string, unknown> | null;
  startedAt: string | null;
  updatedAt: string | null;
  denied: Denied[];
}

export function foldTrials(events: readonly PlanEvent[]): Trial[] {
  const byId = new Map<string, Trial>();
  const get = (id: string, at: string | null): Trial => {
    let t = byId.get(id);
    if (!t) {
      t = { rehearsalId: id, trialId: null, role: null, attempt: null, task: null, phase: 'requested', verdict: null, reason: null, digest: null, verifier: null, startedAt: at, updatedAt: at, denied: [] };
      byId.set(id, t);
    }
    t.updatedAt = at ?? t.updatedAt;
    return t;
  };
  for (const e of events) {
    const d = asRecord(e.data);
    const rid = str(d.rehearsal_id);
    if (!rid) continue;
    if (e.kind === 'trial.started') {
      const t = get(rid, e.at);
      t.role = str(d.role) ?? t.role;
      t.attempt = num(d.attempt) ?? t.attempt;
      t.task = str(d.task) ?? t.task;
      t.startedAt = e.at;
    } else if (e.kind === 'trial.proposed') {
      const t = get(rid, e.at);
      t.trialId = str(d.trial_id) ?? t.trialId;
      t.digest = str(d.artifact_digest) ?? t.digest;
      if (t.phase === 'requested') t.phase = 'proposed';
    } else if (e.kind === 'tool.denied') {
      const t = get(rid, e.at);
      t.trialId = str(d.trial_id) ?? t.trialId;
      t.denied.push({ seq: num(d.seq), tool: str(d.tool), rule: str(d.rule), at: str(d.at) ?? e.at, eventId: e.id });
    } else if (e.kind === 'trial.verdict') {
      const t = get(rid, e.at);
      t.trialId = str(d.trial_id) ?? t.trialId;
      t.attempt = num(d.attempt) ?? t.attempt;
      t.verdict = str(d.verdict);
      t.reason = str(d.reason);
      t.digest = str(d.artifact_digest) ?? t.digest;
      t.verifier = typeof d.verifier === 'object' && d.verifier !== null ? asRecord(d.verifier) : null;
      t.phase = 'verdict';
    }
  }
  return [...byId.values()].sort((a, b) => (b.startedAt ?? '').localeCompare(a.startedAt ?? ''));
}

export function eventsOf(events: readonly PlanEvent[], kinds: readonly string[]): PlanEvent[] {
  return events.filter((e) => kinds.includes(e.kind)).slice().reverse();
}

/** Short one-line summary of a verifier dict: known keys first, never the whole object. */
export function verifierSummary(v: Record<string, unknown> | null): string {
  if (!v) return '—';
  const parts: string[] = [];
  for (const k of ['passed', 'failed', 'total', 'reward', 'status', 'verdict', 'tests']) {
    const val = v[k];
    if (typeof val === 'string' || typeof val === 'number' || typeof val === 'boolean') parts.push(`${k}=${String(val)}`);
  }
  return parts.length ? parts.join(' · ') : `키 ${Object.keys(v).slice(0, 4).join(', ')}`;
}
