/**
 * Idempotency keys (CONTRACT §7: every write carries `Idempotency-Key`).
 * A key is stable per scope within this tab, so a retry after a network failure
 * or a reload sends the same key and the server returns the first handle (A8).
 */
const PREFIX = 'bravo.idem.';

export function uuid(): string {
  if (typeof crypto.randomUUID === 'function' && globalThis.isSecureContext) return crypto.randomUUID();
  // Non-secure origins (LAN IP over http) lack randomUUID; build a v4 UUID by hand.
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = ((b[6] ?? 0) & 0x0f) | 0x40;
  b[8] = ((b[8] ?? 0) & 0x3f) | 0x80;
  const h = Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

export function idemKey(scope: string): string {
  const k = PREFIX + scope;
  try {
    const existing = sessionStorage.getItem(k);
    if (existing) return existing;
    const fresh = uuid();
    sessionStorage.setItem(k, fresh);
    return fresh;
  } catch {
    return uuid();
  }
}

/** Forget the key after a definitive server answer so the next click is a new intent. */
export function releaseIdemKey(scope: string): void {
  try {
    sessionStorage.removeItem(PREFIX + scope);
  } catch {
    /* storage unavailable: nothing to release */
  }
}
