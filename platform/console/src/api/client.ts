/**
 * Hand-written client for the pilot API (CONTRACT §7).
 * Auth: the console sends the Access JWT in `Cf-Access-Jwt-Assertion`.
 * Writes: every POST carries `Idempotency-Key`.
 * SSE: EventSource cannot set headers, so the stream uses fetch via @microsoft/fetch-event-source.
 */
import { fetchEventSource } from '@microsoft/fetch-event-source';
import { asObj, asStr, parseApproval, parseDevTokens, parseEvent, parseGate, parsePlan, parseRun } from './normalize';
import type { Approval, ApprovalInput, CreatePlanInput, DevToken, GateResult, Plan, PlanEvent, Run } from './types';
import { parseEnvironments, parseMe, parsePlans, type EnvSummary, type Me, type PlanSummary } from './home';

export type StreamStatus = 'connecting' | 'open' | 'reconnecting' | 'closed' | 'denied';

export interface StreamHandlers {
  onEvent: (e: PlanEvent) => void;
  onStatus: (s: StreamStatus, detail?: string) => void;
}

export interface ApiClient {
  getDevTokens(): Promise<DevToken[]>;
  /** Who is logged in (Cloudflare Access identity, or the dev token when one is set). */
  getMe(token: string): Promise<Me>;
  listPlans(token: string): Promise<PlanSummary[]>;
  listEnvironments(token: string): Promise<EnvSummary[]>;
  /** GET /v1/plans/{id}/changes: change approvals' binding (diff, image digest, policy diff). */
  listChanges(token: string, planId: string): Promise<Record<string, unknown>[]>;
  createPlan(token: string, input: CreatePlanInput, idemKey: string): Promise<{ plan_id: string }>;
  getPlan(token: string, planId: string): Promise<Plan>;
  runGate(token: string, planId: string, idemKey: string): Promise<GateResult | null>;
  approve(token: string, planId: string, input: ApprovalInput, idemKey: string): Promise<Approval | null>;
  shift(token: string, planId: string, idemKey: string): Promise<{ run_id: string | null; reason: string | null; run: Run | null }>;
  /** Returns an unsubscribe function. `lastEventId` resumes after that event. */
  subscribe(token: string, planId: string, lastEventId: string | null, h: StreamHandlers): () => void;
}

/** Error with the HTTP status and the server reason code (e.g. approval_stale). */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  constructor(status: number, code: string | null, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

const REASON_TEXT: Record<string, string> = {
  approval_stale: '승인 뒤 계획 revision이 바뀌었거나 승인이 만료되었습니다. 다시 승인받아야 합니다.',
  approval_missing: '이 단계의 유효한 승인이 없습니다.',
  security_hold: '보안 보류가 걸려 있어 시작할 수 없습니다.',
  run_active: '같은 계획에 진행 중인 실행이 있습니다.',
  approval_request_used: '이 승인 요청은 이미 결정되었습니다.',
  idempotency_conflict: '같은 Idempotency-Key에 다른 입력이 왔습니다.',
  idempotency_retry: '같은 Idempotency-Key에 다른 입력이 왔습니다.',
  idempotency_in_progress: '같은 요청을 아직 처리하고 있습니다. 잠시 뒤 결과를 확인하세요.',
  separation_of_duties: '계획을 만든 사람이나 승인을 요청한 사람은 승인할 수 없습니다(직무 분리).',
  insufficient_scope: '이 역할로는 할 수 없는 동작입니다.',
  state_mismatch: '지금 계획 상태에서는 할 수 없는 동작입니다.',
  version_conflict: '다른 요청이 계획을 먼저 바꿨습니다. 화면을 새로 받은 뒤 다시 하세요.',
  source_equals_target: '출발지와 목적지가 같은 환경입니다.',
  token_expired: '토큰이 만료되었습니다.',
  wrong_audience: '콘솔용 토큰이 아닙니다(AUD 불일치).',
  not_a_member: '이 테넌트의 구성원이 아닙니다.',
};

/** Plain Korean text for an error shown on screen. */
export function errorText(err: unknown): string {
  if (err instanceof ApiError) {
    const known = err.code ? REASON_TEXT[err.code] : undefined;
    if (known) return `${known} (${err.code}${err.message && err.message !== err.code ? `, ${err.message}` : ''})`;
    if (err.status === 401) return '인증 토큰이 없거나 만료되었습니다. 사용자를 다시 고르세요. (401)';
    if (err.status === 403) return `이 사용자 역할로는 할 수 없습니다. (403${err.code ? `, ${err.code}` : ''})`;
    if (err.status === 409) return `요청이 현재 상태와 맞지 않습니다. (409${err.code ? `, ${err.code}` : ''})`;
    if (err.status === 0) return `API에 연결하지 못했습니다. ${err.message}`;
    return `${err.message} (${err.status}${err.code ? `, ${err.code}` : ''})`;
  }
  if (err instanceof Error) return err.message;
  return '알 수 없는 오류입니다.';
}

/**
 * The API answers errors as {error: <reason code>, detail?: {...}} (rest.py error_response).
 * FastAPI validation errors use {detail: [...]}; those show as a plain message.
 */
function reasonFrom(body: unknown): { code: string | null; message: string | null } {
  const o = asObj(body);
  if (!o) return { code: null, message: null };
  const d = asObj(o.detail);
  const code = asStr(o.error) ?? asStr(o.code) ?? asStr(d?.code) ?? null;
  let message: string | null = null;
  if (d) {
    const parts = Object.entries(d).map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`);
    message = parts.length ? parts.join(', ') : null;
  } else if (Array.isArray(o.detail)) {
    message = '요청 형식이 맞지 않습니다.';
  }
  return { code, message };
}

class FatalStreamError extends Error {}
class RetryStreamError extends Error {}

export function createHttpClient(base: string): ApiClient {
  const root = base.replace(/\/+$/, '');

  async function request(
    method: 'GET' | 'POST',
    path: string,
    opts: { token?: string; body?: unknown; idemKey?: string } = {},
  ): Promise<unknown> {
    const headers: Record<string, string> = { Accept: 'application/json' };
    if (opts.token) headers['Cf-Access-Jwt-Assertion'] = opts.token;
    if (opts.idemKey) headers['Idempotency-Key'] = opts.idemKey;
    if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
    let res: Response;
    try {
      res = await fetch(`${root}${path}`, {
        method,
        headers,
        body: opts.body === undefined ? null : JSON.stringify(opts.body),
        // same-origin: behind Cloudflare Access the CF_Authorization cookie must reach the API so
        // the edge can add Cf-Access-Jwt-Assertion. Cross-origin (dev) calls still send no cookie.
        credentials: 'same-origin',
      });
    } catch (e) {
      throw new ApiError(0, null, e instanceof Error ? e.message : String(e));
    }
    const text = await res.text();
    let json: unknown = null;
    if (text) {
      try {
        json = JSON.parse(text);
      } catch {
        json = null;
      }
    }
    if (!res.ok) {
      const { code, message } = reasonFrom(json);
      throw new ApiError(res.status, code, message ?? code ?? (res.statusText || '요청 실패'));
    }
    return json;
  }

  const enc = encodeURIComponent;

  return {
    async getDevTokens() {
      return parseDevTokens(await request('GET', '/v1/dev/tokens'));
    },

    async getMe(token) {
      return parseMe(await request('GET', '/v1/me', { token }));
    },

    async listPlans(token) {
      return parsePlans(await request('GET', '/v1/plans', { token }));
    },

    async listEnvironments(token) {
      return parseEnvironments(await request('GET', '/v1/environments', { token }));
    },

    async listChanges(token, planId) {
      const body = asObj(await request('GET', `/v1/plans/${enc(planId)}/changes`, { token }));
      return Array.isArray(body?.changes) ? (body.changes as unknown[]).map((c) => asObj(c) ?? {}) : [];
    },

    async createPlan(token, input, idemKey) {
      const body = await request('POST', '/v1/plans', { token, body: input, idemKey });
      const id = asStr(asObj(body)?.plan_id);
      if (!id) throw new ApiError(500, null, '응답에 plan_id가 없습니다.');
      return { plan_id: id };
    },

    async getPlan(token, planId) {
      return parsePlan(await request('GET', `/v1/plans/${enc(planId)}`, { token }));
    },

    async runGate(token, planId, idemKey) {
      return parseGate(await request('POST', `/v1/plans/${enc(planId)}/gate`, { token, body: {}, idemKey }));
    },

    async approve(token, planId, input, idemKey) {
      return parseApproval(
        await request('POST', `/v1/plans/${enc(planId)}/approvals`, { token, body: input, idemKey }),
      );
    },

    async shift(token, planId, idemKey) {
      const body = await request('POST', `/v1/plans/${enc(planId)}/shift`, {
        token,
        body: { stage: 'cutover' },
        idemKey,
      });
      const o = asObj(body);
      // 202 may carry run_id null with a reason when the Executor did not take the run.
      return { run_id: asStr(o?.run_id), reason: asStr(o?.reason), run: parseRun(body) };
    },

    subscribe(token, planId, lastEventId, h) {
      const ctrl = new AbortController();
      let seq = 0;
      // No token: rely on the Access cookie (same origin); the edge adds the assertion header.
      const headers: Record<string, string> = token ? { 'Cf-Access-Jwt-Assertion': token } : {};
      // The library keys this header in lower case and updates it on every `id:` line.
      if (lastEventId) headers['last-event-id'] = lastEventId;
      h.onStatus('connecting');
      fetchEventSource(`${root}/v1/plans/${enc(planId)}/events`, {
        headers,
        credentials: 'same-origin',
        signal: ctrl.signal,
        openWhenHidden: true,
        async onopen(res) {
          const type = res.headers.get('content-type') ?? '';
          if (res.ok && type.startsWith('text/event-stream')) {
            h.onStatus('open');
            return;
          }
          if (res.status === 401 || res.status === 403 || res.status === 404) {
            throw new FatalStreamError(`이벤트 흐름 거부 (${res.status})`);
          }
          throw new RetryStreamError(`이벤트 흐름 응답 ${res.status}`);
        },
        onmessage(msg) {
          if (!msg.data) return;
          seq += 1;
          const e = parseEvent(msg.data, msg.id || `local-${seq}`);
          if (e) h.onEvent(e);
        },
        onclose() {
          // Server ended the stream: reconnect with Last-Event-ID.
          throw new RetryStreamError('서버가 이벤트 흐름을 닫음');
        },
        onerror(err) {
          if (err instanceof FatalStreamError) {
            h.onStatus('denied', err.message);
            throw err;
          }
          h.onStatus('reconnecting', err instanceof Error ? err.message : undefined);
          return 2000;
        },
      }).catch(() => {
        // Fatal errors are already reported through onStatus; abort ends here too.
      });
      return () => {
        ctrl.abort();
        h.onStatus('closed');
      };
    },
  };
}
