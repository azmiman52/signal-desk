import { useEffect, useRef, useState, type FormEvent } from 'react';
import { ApiError, mutationKey, request, type Project, type Session } from './api';
import type { SessionFence } from './session-fence';
import { canCollect, displayDate, isSubscription, safeGithubUrl, sourceStatus, type CollectionRun, type Release, type Subscription } from './sources-model';
import { useOwnedPage } from './use-owned-page';
import { ReleaseContent } from './ReleaseContent';

type Props = { project: Project; session: Session | null; enabled: boolean; fence: SessionFence; onRevalidate: () => void; onClose: () => void };
type SourceResult = { account_id: string; subscription?: Subscription; run?: CollectionRun; reused?: boolean; restored?: boolean };
function Paging({ page, next, disabled, setPage, label }: { page: number; next: boolean; disabled: boolean; setPage: (value: number) => void; label: string }) {
  return <nav className="pagination" aria-label={label}><button className="secondary" disabled={page === 1 || disabled} onClick={() => setPage(page - 1)}>Previous</button><span>Page {page}</span><button className="secondary" disabled={!next || disabled} onClick={() => setPage(page + 1)}>Next</button></nav>;
}
export function SourcesPanel({ project, session, enabled, fence, onRevalidate, onClose }: Props) {
  const [repository, setRepository] = useState('');
  const [prereleases, setPrereleases] = useState(false);
  const [page, setPage] = useState(1);
  const [includeRemoved, setIncludeRemoved] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [runPage, setRunPage] = useState(1);
  const [releasePage, setReleasePage] = useState(1);
  const [releaseId, setReleaseId] = useState<string | null>(null);
  const [release, setRelease] = useState<(Release & { body: string | null }) | null>(null);
  const [releaseLoading, setReleaseLoading] = useState(false);
  const [releaseError, setReleaseError] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [retryAt, setRetryAt] = useState<Record<string, string | null>>({});
  const [now, setNow] = useState(Date.now());
  const [conflict, setConflict] = useState<Subscription | null>(null);
  const [overrides, setOverrides] = useState<Record<string, Subscription>>({});
  const [preferences, setPreferences] = useState<Record<string, { value: boolean; version: number }>>({});
  const lock = useRef(false);
  const attempt = useRef<{ fingerprint: string; key: string } | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const sourcePage = useOwnedPage<Subscription>(`/projects/${project.id}/subscriptions?page=${page}&page_size=10&include_removed=${includeRemoved}`, session, enabled, fence, refresh, onRevalidate);
  const runs = useOwnedPage<CollectionRun>(selected ? `/subscriptions/${selected}/runs?page=${runPage}&page_size=10` : null, session, enabled, fence, refresh, onRevalidate);
  const releases = useOwnedPage<Release>(selected ? `/subscriptions/${selected}/releases?page=${releasePage}&page_size=10` : null, session, enabled, fence, refresh, onRevalidate);
  const sources = sourcePage.data?.items.map(s => overrides[s.id]?.version > s.version ? overrides[s.id] : s) ?? [];
  const activeSource = sources.find(s => s.id === selected);
  const hasActiveRun = sources.some(s => !!s.active_run_id) || runs.data?.items.some(r => ['queued', 'running', 'retry_wait'].includes(r.state));

  useEffect(() => { heading.current?.focus(); }, []);
  useEffect(() => { if (!enabled || !hasActiveRun) return; const timer = window.setInterval(() => setRefresh(v => v + 1), 4000); return () => window.clearInterval(timer); }, [enabled, hasActiveRun]);
  useEffect(() => { if (!enabled || !Object.values(retryAt).some(Boolean)) return; const timer = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(timer); }, [enabled, retryAt]);
  useEffect(() => {
    if (!releaseId || !selected || !session || !enabled) return;
    const abort = new AbortController(), generation = fence.ticket();
    const current = () => !abort.signal.aborted && fence.current(generation);
    setRelease(null); setReleaseLoading(true); setReleaseError('');
    void request<{ account_id: string; release: Release & { body: string | null } }>(`/subscriptions/${selected}/releases/${releaseId}`, { signal: abort.signal }).then(value => {
      if (!current()) return;
      if (!fence.accepts(generation, session.account.id, value.account_id)) { onRevalidate(); return; }
      setRelease(value.release);
    }).catch(e => { if (!current()) return; setReleaseError((e as Error).message); if (e instanceof ApiError && e.status === 401) onRevalidate(); }).finally(() => { if (current()) setReleaseLoading(false); });
    return () => abort.abort();
  }, [releaseId, selected, session, enabled, fence, onRevalidate, refresh]);

  function selectSource(id: string) { setSelected(id); setRunPage(1); setReleasePage(1); setReleaseId(null); setRelease(null); }
  async function mutate(path: string, method: string, body: unknown, label: string, scope: string, clearForm = false) {
    if (!session || !enabled || lock.current) return;
    const generation = fence.ticket();
    lock.current = true; setBusy(true); setError('');
    attempt.current = mutationKey(attempt.current, JSON.stringify({ path, method, body }));
    try {
      const value = await request<SourceResult | Subscription>(path, { method, body, csrf: session.csrf_token, key: attempt.current.key });
      if (!fence.current(generation)) return;
      if (!fence.accepts(generation, session.account.id, value.account_id)) { onRevalidate(); return; }
      attempt.current = null; setConflict(null); setRetryAt(v => ({ ...v, [scope]: null })); setPage(1); setRunPage(1); setReleasePage(1); setRefresh(v => v + 1);
      const sub = isSubscription(value) ? value : value.subscription;
      if (sub) { setOverrides(v => ({ ...v, [sub.id]: sub })); setPreferences(v => ({ ...v, [sub.id]: { value: clearForm || method === 'PATCH' ? sub.include_prereleases : v[sub.id]?.value ?? sub.include_prereleases, version: sub.version } })); }
      if (clearForm) { setRepository(''); setPrereleases(false); if (sub) selectSource(sub.id); }
      if ('run' in value && value.run) {
        setNotice(`${value.reused ? 'Existing collection run reused' : label}. Run status: ${value.run.state}. Completion is shown in collection history.`);
      } else setNotice(label);
    } catch (e) {
      if (!fence.current(generation)) return;
      const failure = e as ApiError<unknown>; setError(failure.code === 'capacity_exceeded' ? 'Your account has reached its source limit. Review existing sources before adding or resuming another.' : failure.message);
      if (failure.retryAt) setRetryAt(v => ({ ...v, [scope]: failure.retryAt }));
      if (failure.code === 'version_conflict' && isSubscription(failure.latest)) setConflict(failure.latest);
      if (failure.status === 401 || failure.code === 'csrf_failed') onRevalidate();
      if (failure.status > 0 && failure.status < 500) attempt.current = null;
    } finally { lock.current = false; setBusy(false); }
  }
  function add(event: FormEvent) {
    event.preventDefault();
    if (!repository.trim()) { setError('Enter a public GitHub repository.'); return; }
    void mutate(`/projects/${project.id}/subscriptions`, 'POST', { repository: repository.trim(), include_prereleases: prereleases }, 'Source saved; initial collection requested', 'add', true);
  }
  function resolveConflict() {
    if (!conflict) return;
    setOverrides(v => ({ ...v, [conflict.id]: conflict }));
    setPreferences(v => ({ ...v, [conflict.id]: { value: v[conflict.id]?.value ?? conflict.include_prereleases, version: conflict.version } }));
    setConflict(null); setError(''); setNotice('Latest source version loaded. Your preference draft is preserved; review and submit the intended action again.');
  }
  return <section className="sources-panel" hidden={!enabled} aria-labelledby="sources-heading">
    <div className="section-heading"><div><p className="eyebrow">Project sources</p><h2 id="sources-heading" ref={heading} tabIndex={-1}>{project.name}</h2></div><button className="secondary" disabled={busy} onClick={onClose}>Close sources</button></div>
    <p className="scope-note">Manual collection only. Each scan checks up to the first 30 releases returned by GitHub, before prerelease filtering. Older releases may not have been captured. Scheduling and review decisions arrive in later stages.</p>
    {project.archived_at ? <p className="scope-note">This project is archived. History is available; restore the project to change sources or collect releases.</p> : <form className="status-card source-add" onSubmit={add}><h3>Add a public repository</h3><label htmlFor="repository">GitHub repository</label><input id="repository" autoComplete="off" value={repository} onChange={e => setRepository(e.target.value)} disabled={busy} required maxLength={300} placeholder="owner/repository or https://github.com/owner/repository" /><label className="checkbox"><input type="checkbox" checked={prereleases} onChange={e => setPrereleases(e.target.checked)} disabled={busy} />Include prereleases for this project</label><p className="muted">Stable releases are included by default. Captured prereleases remain visible in source history. GitHub must validate the repository before it is saved.</p><button disabled={busy || !!(retryAt.add && new Date(retryAt.add).getTime() > now)}>{busy ? 'Request in progress…' : 'Connect repository'}</button>{retryAt.add && new Date(retryAt.add).getTime() > now && <p role="status">Try again after {displayDate(retryAt.add)}.</p>}</form>}
    <div role="status" aria-live="polite">{notice}</div>{error && <p className="error" role="alert">{error}</p>}
    {conflict && <div className="conflict"><h3>This source changed</h3><p>Latest version {conflict.version}: {conflict.source.repository}, {conflict.state}, prereleases {conflict.include_prereleases ? 'included' : 'excluded'}.</p><button className="secondary" onClick={resolveConflict}>Load latest source, keep my draft</button></div>}
    <div className="section-heading"><label className="checkbox"><input type="checkbox" checked={includeRemoved} onChange={e => { setIncludeRemoved(e.target.checked); setPage(1); }} disabled={busy} />Include removed sources</label><button className="secondary" disabled={busy || sourcePage.loading} onClick={() => setRefresh(v => v + 1)}>Refresh sources</button></div>
    {sourcePage.error && <p className="error" role="alert">{sourcePage.error}</p>}{sourcePage.loading && <p role="status">Refreshing sources…</p>}
    {!sourcePage.loading && !sourcePage.error && sources.length === 0 && <div className="status-card empty"><h3>No sources on this page</h3><p>Add a public repository to start collecting releases.</p>{page > 1 && <button onClick={() => setPage(1)}>First page</button>}</div>}
    <div className="source-list">{sources.map(source => {
      const preference = preferences[source.id] ?? { value: source.include_prereleases, version: source.version };
      const sourceUrl = safeGithubUrl(source.source.url);
      const disabled = busy || !!project.archived_at || !!conflict || sourcePage.loading;
      return <article className="status-card source-card" key={source.id}>
        <div className="section-heading"><h3>{sourceUrl ? <a href={sourceUrl} target="_blank" rel="noopener noreferrer">{source.source.repository} ↗</a> : source.source.repository}</h3><span className="badge">{sourceStatus(source)}</span></div>
        <p className="muted">Last successful check: {displayDate(source.last_success_at)}. Manual checks only.</p>
        {source.connection_state === 'pending' && <p>Repository saved. A successful fetch is still required before it is connected.</p>}
        {source.connection_state === 'attention' && <p className="scope-note">The latest collection needs attention. Previously collected releases and the last successful check are retained.</p>}
        {source.state !== 'removed' && <><label className="checkbox"><input type="checkbox" checked={preference.value} disabled={disabled} onChange={e => setPreferences(v => ({ ...v, [source.id]: { value: e.target.checked, version: preference.version } }))} />Include prereleases</label><div className="form-actions"><button className="secondary" disabled={disabled || preference.value === source.include_prereleases} onClick={() => void mutate(`/subscriptions/${source.id}`, 'PATCH', { include_prereleases: preference.value, expected_version: preference.version }, 'Source preference saved for future delivery; existing history is unchanged.', source.id)}>Save preference</button><button disabled={!canCollect(project, source, disabled, retryAt[source.id], now)} onClick={() => { selectSource(source.id); void mutate(`/subscriptions/${source.id}/checks`, 'POST', {}, 'Manual check requested', source.id); }}>{source.active_run_id ? 'Check in progress' : 'Check now'}</button><button className="secondary" disabled={disabled} onClick={() => void mutate(`/subscriptions/${source.id}/${source.state === 'paused' ? 'resume' : 'pause'}`, 'POST', { expected_version: source.version }, source.state === 'paused' ? 'Source resumed. Manual checks are available.' : 'Source paused. Collected history is preserved.', source.id)}>{source.state === 'paused' ? 'Resume' : 'Pause'}</button><button className="secondary" disabled={disabled} onClick={() => void mutate(`/subscriptions/${source.id}/remove`, 'POST', { expected_version: source.version }, 'Source removed from this project. History is preserved.', source.id)}>Remove source</button></div></>}
        {source.state === 'removed' && <p>To follow this repository again, add it above. Prior history is preserved.</p>}
        {retryAt[source.id] && new Date(retryAt[source.id]!).getTime() > now && <p role="status">Manual check available after {displayDate(retryAt[source.id])}.</p>}
        <button className="secondary history-button" onClick={() => selectSource(source.id)}>View collection & release history</button>
      </article>;
    })}</div>
    {sourcePage.data && <Paging label="Source pages" page={page} next={sourcePage.data.has_next} disabled={busy || sourcePage.loading} setPage={setPage} />}
    {selected && <section className="source-history" aria-labelledby="history-heading"><h3 id="history-heading">History {activeSource ? `· ${activeSource.source.repository}` : ''}</h3><p className="muted">Counts describe public source releases, not another project’s review activity.</p><h4>Collection runs</h4>{runs.loading && <p role="status">Refreshing collection history…</p>}{runs.error && <p className="error" role="alert">{runs.error}</p>}{!runs.loading && runs.data?.items.length === 0 && <p>No collection runs yet.</p>}{runs.data?.items.map(run => <article key={run.id} className="run-card"><div className="section-heading"><strong>{run.state.replaceAll('_', ' ')}</strong><span className="muted">{displayDate(run.created_at ?? run.started_at)}</span></div><p>Discovered {run.counts.discovered} · New {run.counts.created} · Updated {run.counts.updated} · Skipped {run.counts.skipped}</p><p className="muted">Coverage: {run.coverage} · Attempt {run.attempt_count}</p>{run.state === 'succeeded' && run.counts.discovered === 0 && <p>Successful check: GitHub returned no releases.</p>}{run.state === 'succeeded' && run.counts.discovered > 0 && run.counts.created === 0 && run.counts.updated === 0 && <p>No new or changed releases in this bounded check.</p>}{run.state === 'partial' && <p className="scope-note">Partial collection. Usable entries may be retained; this is not a full successful check.</p>}{run.error && <p className="error">{run.error.message}</p>}{run.retry_at && <p>Provider permits retry after {displayDate(run.retry_at)}. Request a manual check when eligible.</p>}</article>)}{runs.data && <Paging label="Collection run pages" page={runPage} next={runs.data.has_next} disabled={runs.loading} setPage={setRunPage} />}
      <h4>Collected releases</h4><p className="muted">Bounded retained history, including captured prereleases. Review decisions are not available yet.</p>{releases.loading && <p role="status">Refreshing releases…</p>}{releases.error && <p className="error" role="alert">{releases.error}</p>}{!releases.loading && releases.data?.items.length === 0 && <p>{activeSource?.connection_state === 'connected' ? 'No releases retained after the successful check.' : 'No releases retained yet. Inspect collection history for pending or failed checks.'}</p>}{releases.data?.items.map(item => <article key={item.id} className="release-card"><div><h4>{item.title || item.tag_name}</h4><p className="muted">{item.tag_name} · {item.prerelease ? 'Prerelease' : 'Stable'} · Published {displayDate(item.published_at)}</p></div><button className="secondary" onClick={() => setReleaseId(item.id)}>Read release notes</button></article>)}{releases.data && <Paging label="Release pages" page={releasePage} next={releases.data.has_next} disabled={releases.loading} setPage={setReleasePage} />}
      {releaseId && <section className="status-card release-detail" aria-label="Release notes"><div className="section-heading"><h3>Release notes</h3><button className="secondary" onClick={() => { setReleaseId(null); setRelease(null); }}>Close notes</button></div>{releaseLoading && <p role="status">Loading release notes…</p>}{releaseError && <p className="error" role="alert">{releaseError}</p>}{release && <ReleaseContent release={release} />}</section>}
    </section>}
  </section>;
}
