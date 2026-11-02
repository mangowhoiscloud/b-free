/** Small shared pieces: icons, verdict marks, badges, confirm dialog. */
import { useEffect, useId, useRef, type ReactNode } from 'react';
import type { ActorKind, ControlValue, EventMode, PlanEvent } from '../api/types';

type IconProps = { size?: number };

export function IconPass({ size = 16 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" aria-hidden="true" focusable="false">
      <circle cx="8" cy="8" r="7" fill="none" stroke="currentColor" strokeWidth="1.6" />
      <path d="M4.6 8.3l2.3 2.3 4.6-4.9" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
export function IconBlock({ size = 16 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" aria-hidden="true" focusable="false">
      <rect x="1" y="1" width="14" height="14" rx="2" fill="none" stroke="currentColor" strokeWidth="1.6" />
      <path d="M5 5l6 6M11 5l-6 6" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}
export function IconUnknown({ size = 16 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" aria-hidden="true" focusable="false">
      <path d="M8 1l7 7-7 7-7-7z" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
      <path d="M6.3 6.4a1.8 1.8 0 1 1 2.4 1.7c-.5.2-.7.6-.7 1.1" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      <circle cx="8" cy="11.3" r=".9" fill="currentColor" />
    </svg>
  );
}
export function IconAlert({ size = 18 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 18 18" aria-hidden="true" focusable="false">
      <path d="M9 1.5l8 14.5H1z" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
      <path d="M9 7v4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      <circle cx="9" cy="13.4" r="1" fill="currentColor" />
    </svg>
  );
}

export type Tone = 'pass' | 'block' | 'unknown' | 'neutral';

/** Verdict mark: icon + text, never color alone. */
export function Mark({ tone, children, size }: { tone: Tone; children: ReactNode; size?: 'lg' }) {
  const Icon = tone === 'pass' ? IconPass : tone === 'block' ? IconBlock : IconUnknown;
  return (
    <span className={`mark mark--${tone}${size ? ' mark--lg' : ''}`}>
      {tone !== 'neutral' && <Icon size={size ? 22 : 15} />}
      <span>{children}</span>
    </span>
  );
}

export function ControlValueMark({ value }: { value: ControlValue }) {
  const tone: Tone = value === 'PASS' ? 'pass' : value === 'FAIL' ? 'block' : 'unknown';
  return <Mark tone={tone}>{value}</Mark>;
}

const MODE_TEXT: Record<EventMode, string> = { live: 'live', recorded: 'recorded', unknown: 'mode ?' };
const MODE_TITLE: Record<EventMode, string> = {
  live: '실제 시스템에서 일어난 단계',
  recorded: '녹화된 실행을 다시 보인 단계',
  unknown: '서버가 live, recorded가 아닌 mode 값을 보냄',
};

export function ModeBadge({ mode }: { mode: EventMode }) {
  return (
    <span className={`badge badge--${mode}`} title={MODE_TITLE[mode]}>
      {MODE_TEXT[mode]}
    </span>
  );
}

const ACTOR_TEXT: Record<ActorKind, string> = { console: '콘솔', mcp: 'MCP로 요청됨', system: '시스템' };

export function ActorBadge({ actor }: { actor: PlanEvent['actor'] }) {
  return (
    <span className={`actor actor--${actor.kind}`}>
      <span className="actor__kind">{ACTOR_TEXT[actor.kind]}</span>
      {actor.principal && <span className="actor__who">{actor.principal}</span>}
    </span>
  );
}

export function Panel({
  id,
  title,
  step,
  actions,
  children,
  className,
}: {
  id: string;
  title: string;
  step?: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section id={id} tabIndex={-1} className={`panel ${className ?? ''}`} aria-labelledby={`${id}-title`}>
      <header className="panel__head">
        <h2 id={`${id}-title`} className="panel__title">
          {step && <span className="panel__step">{step}</span>}
          {title}
        </h2>
        {actions && <div className="panel__actions">{actions}</div>}
      </header>
      {children}
    </section>
  );
}

/** Lists why an action button is disabled, linked by aria-describedby. */
export function Reasons({ id, reasons }: { id: string; reasons: string[] }) {
  if (reasons.length === 0) return null;
  return (
    <ul id={id} className="reasons">
      {reasons.map((r) => (
        <li key={r}>{r}</li>
      ))}
    </ul>
  );
}

export function ErrorLine({ text }: { text: string | null }) {
  if (!text) return null;
  return (
    <p className="error-line" role="alert">
      <IconAlert size={16} />
      <span>{text}</span>
    </p>
  );
}

/** Modal confirm built on <dialog>: focus trap and Esc come from the browser. */
export function ConfirmDialog({
  open,
  title,
  confirmText,
  onConfirm,
  onCancel,
  children,
  busy,
}: {
  open: boolean;
  title: string;
  confirmText: string;
  onConfirm: () => void;
  onCancel: () => void;
  children: ReactNode;
  busy?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  return (
    <dialog ref={ref} className="dialog" aria-labelledby={titleId} onCancel={onCancel} onClose={onCancel}>
      <h3 id={titleId} className="dialog__title">
        {title}
      </h3>
      <div className="dialog__body">{children}</div>
      <div className="dialog__actions">
        <button type="button" className="btn btn--ghost" onClick={onCancel} autoFocus>
          취소
        </button>
        <button type="button" className="btn btn--primary" onClick={onConfirm} disabled={busy}>
          {confirmText}
        </button>
      </div>
    </dialog>
  );
}
