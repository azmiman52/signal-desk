import { useEffect, useRef, useState } from 'react';
import { ApiError, request, type Session } from './api';
import type { SessionFence } from './session-fence';
import type { OwnedPage } from './sources-model';

export function useOwnedPage<T>(path: string | null, session: Session | null, enabled: boolean, fence: SessionFence, refresh: number, onRevalidate: () => void) {
  const [data, setData] = useState<OwnedPage<T> | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const lastPath = useRef(path);
  useEffect(() => {
    if (!path || !session || !enabled) return;
    const abort = new AbortController();
    const generation = fence.ticket();
    const current = () => !abort.signal.aborted && fence.current(generation);
    if (lastPath.current !== path) setData(null);
    lastPath.current = path;
    setLoading(true); setError('');
    void request<OwnedPage<T>>(path, { signal: abort.signal }).then(value => {
      if (!current()) return;
      if (typeof value.account_id !== 'string') throw new ApiError(503, 'invalid_response', 'Response identity could not be verified. Refresh or contact the server operator.');
      if (!fence.accepts(generation, session.account.id, value.account_id)) { setData(null); onRevalidate(); return; }
      setData(value);
    }).catch(e => {
      if (!current()) return;
      setError((e as Error).message);
      if (e instanceof ApiError && e.status === 401) { setData(null); onRevalidate(); }
    }).finally(() => { if (current()) setLoading(false); });
    return () => abort.abort();
  }, [path, session, enabled, fence, refresh, onRevalidate]);
  return { data: lastPath.current === path ? data : null, loading, error };
}
