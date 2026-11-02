/**
 * Screen state in the URL (DESIGN-REFERENCE §3/§5, Argo CD and Grafana): `#/<screen>?range=1h&env=a`.
 * Filters, time range and the selected environment survive reloads and can be shared as a link.
 */
import { useCallback, useEffect, useState } from 'react';

export function hashParts(): { screen: string; params: URLSearchParams } {
  const raw = window.location.hash.replace(/^#\/?/, '');
  const [screen = '', query = ''] = raw.split('?');
  return { screen, params: new URLSearchParams(query) };
}

export function setHashParams(update: Record<string, string | null>): void {
  const { screen, params } = hashParts();
  for (const [k, v] of Object.entries(update)) {
    if (v === null || v === '') params.delete(k);
    else params.set(k, v);
  }
  const q = params.toString();
  history.replaceState(null, '', `${window.location.pathname}${window.location.search}#/${screen}${q ? `?${q}` : ''}`);
  window.dispatchEvent(new Event('bravo-hashparams'));
}

/** One URL parameter as React state. */
export function useHashParam(key: string, fallback: string): [string, (v: string | null) => void] {
  const read = useCallback(() => hashParts().params.get(key) ?? fallback, [key, fallback]);
  const [value, setValue] = useState(read);
  useEffect(() => {
    const on = () => setValue(read());
    window.addEventListener('hashchange', on);
    window.addEventListener('bravo-hashparams', on);
    return () => {
      window.removeEventListener('hashchange', on);
      window.removeEventListener('bravo-hashparams', on);
    };
  }, [read]);
  return [value, useCallback((v: string | null) => setHashParams({ [key]: v }), [key])];
}
