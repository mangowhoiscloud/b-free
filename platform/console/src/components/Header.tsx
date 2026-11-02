import { useEffect, useId, useRef, useState, type ChangeEvent, type FormEvent } from 'react';
import { useMutation } from '@tanstack/react-query';
import { ApiError, errorText } from '../api/client';
import type { CreatePlanInput, Role } from '../api/types';
import { idemKey, releaseIdemKey } from '../lib/idem';
import { recentPlans, useApi, useTheme, type Session } from '../state';
import { ErrorLine } from './bits';

const ROLE_TEXT: Record<Role, string> = { operator: '운영자', approver: '승인자', viewer: '조회자' };

export function Header({ session }: { session: Session }) {
  const [theme, setTheme] = useTheme();
  const isDark =
    theme === 'dark' || (theme === null && typeof matchMedia === 'function' && matchMedia('(prefers-color-scheme: dark)').matches);

  return (
    <header className="topbar">
      <Identity session={session} />

      <button
        type="button"
        className="btn btn--icon"
        onClick={() => setTheme(isDark ? 'light' : 'dark')}
        aria-label={isDark ? '밝은 화면으로' : '어두운 화면으로'}
        title={isDark ? '밝은 화면으로' : '어두운 화면으로'}
      >
        {isDark ? (
          <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
            <circle cx="9" cy="9" r="3.6" fill="none" stroke="currentColor" strokeWidth="1.6" />
            <path d="M9 1.5v2M9 14.5v2M1.5 9h2M14.5 9h2M3.7 3.7l1.4 1.4M12.9 12.9l1.4 1.4M3.7 14.3l1.4-1.4M12.9 5.1l1.4-1.4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          </svg>
        ) : (
          <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
            <path d="M15 11.2A6.5 6.5 0 0 1 6.8 3a6.5 6.5 0 1 0 8.2 8.2z" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
          </svg>
        )}
      </button>
    </header>
  );
}

export function PlanPicker({ planId, onOpen, session }: { planId: string | null; onOpen: (id: string) => void; session: Session }) {
  const [value, setValue] = useState(planId ?? '');
  const [creating, setCreating] = useState(false);
  const listId = useId();
  useEffect(() => setValue(planId ?? ''), [planId]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const v = value.trim();
    if (v) onOpen(v);
  };

  return (
    <div className="plan-picker">
      <form onSubmit={submit} className="plan-picker__form">
        <label className="field field--inline">
          <span className="field__label">계획 ID</span>
          <input
            className="input mono"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            list={listId}
            placeholder="plan_id 붙여넣기"
            spellCheck={false}
            autoComplete="off"
          />
        </label>
        <datalist id={listId}>
          {recentPlans().map((p) => (
            <option key={p} value={p} />
          ))}
        </datalist>
        <button type="submit" className="btn btn--ghost" disabled={!value.trim() || value.trim() === planId}>
          열기
        </button>
      </form>
      <button type="button" className="btn btn--ghost" onClick={() => setCreating(true)} disabled={session.user?.role !== 'operator'} title={session.user?.role !== 'operator' ? '계획 만들기는 operator 역할만 할 수 있습니다' : undefined}>
        새 계획
      </button>
      <CreatePlanDialog open={creating} onClose={() => setCreating(false)} onCreated={onOpen} session={session} />
    </div>
  );
}

function CreatePlanDialog({
  open,
  onClose,
  onCreated,
  session,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (id: string) => void;
  session: Session;
}) {
  const api = useApi();
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const [input, setInput] = useState<CreatePlanInput>({
    workload: 'chat',
    source_environment_id: '',
    target_environment_id: '',
  });
  const scope = `plan:create:${input.workload}:${input.source_environment_id}:${input.target_environment_id}`;

  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);

  const create = useMutation({
    mutationFn: () => {
      if (!session.user) throw new Error('사용자를 고르세요.');
      return api.createPlan(session.user.token, input, idemKey(scope));
    },
    onSuccess: (r) => {
      releaseIdemKey(scope);
      onCreated(r.plan_id);
      onClose();
    },
    onError: (err) => {
      if (!(err instanceof ApiError && err.status === 0)) releaseIdemKey(scope);
    },
  });

  const valid = input.workload.trim() && input.source_environment_id.trim() && input.target_environment_id.trim();
  const set = (k: keyof CreatePlanInput) => (e: ChangeEvent<HTMLInputElement>) => setInput((p) => ({ ...p, [k]: e.target.value }));

  return (
    <dialog ref={ref} className="dialog" aria-labelledby={titleId} onClose={onClose} onCancel={onClose}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (valid) create.mutate();
        }}
      >
        <h3 id={titleId} className="dialog__title">
          새 이전 계획
        </h3>
        <div className="dialog__body stack">
          <label className="field">
            <span className="field__label">워크로드</span>
            <input className="input" value={input.workload} onChange={set('workload')} required />
          </label>
          <label className="field">
            <span className="field__label">출발지 환경 ID</span>
            <input className="input mono" value={input.source_environment_id} onChange={set('source_environment_id')} required spellCheck={false} />
          </label>
          <label className="field">
            <span className="field__label">목적지 환경 ID</span>
            <input className="input mono" value={input.target_environment_id} onChange={set('target_environment_id')} required spellCheck={false} />
          </label>
          <p className="muted">환경 ID는 <code>POST /v1/environments</code>(MCP <code>env_register</code>)가 돌려준 값입니다. 요청자는 {session.user?.email ?? '—'}입니다.</p>
          <ErrorLine text={create.error ? errorText(create.error) : null} />
        </div>
        <div className="dialog__actions">
          <button type="button" className="btn btn--ghost" onClick={onClose}>
            취소
          </button>
          <button type="submit" className="btn btn--primary" disabled={!valid || create.isPending}>
            {create.isPending ? '만드는 중…' : '계획 만들기'}
          </button>
        </div>
      </form>
    </dialog>
  );
}

function Identity({ session }: { session: Session }) {
  const me = session.me;
  if (me.loading) return <p className="user muted">로그인 확인 중…</p>;
  if (me.data?.email)
    return (
      <p className="user" aria-label="로그인한 사용자">
        <span className="meta">UTC+9</span>
        <span className="user__email">{me.data.email}</span>
        <span className="tag">{me.data.role ? `${ROLE_TEXT[me.data.role]} · ${me.data.roleRaw ?? me.data.role}` : `역할 ${me.data.roleRaw ?? '없음'}`}</span>
      </p>
    );
  return (
    <p className="user muted" role="status">
      로그인 정보 연결 안 됨 · <span className="mono">GET /v1/me</span> · {me.error ? errorText(me.error) : '응답에 이메일 없음'}
    </p>
  );
}
