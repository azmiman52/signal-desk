import { StrictMode, useEffect, useRef, useState, useCallback, type FormEvent } from 'react';
import { createRoot } from 'react-dom/client';
import { ApiError, accountChanged, mutationKey, request, signInError, type Project, type ProjectPage, type Session } from './api';
import { SessionFence } from './session-fence';
import { SourcesPanel } from './SourcesPanel';
import './styles.css';

function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [sessionLoading, setSessionLoading] = useState(true);
  const [sessionError, setSessionError] = useState('');
  const [sessionTick, setSessionTick] = useState(0);
  const [list, setList] = useState<ProjectPage | null>(null);
  const [loading, setLoading] = useState(false);
  const [listError, setListError] = useState('');
  const [page, setPage] = useState(1);
  const [archived, setArchived] = useState(false);
  const [tick, setTick] = useState(0);
  const [editor, setEditor] = useState<Project | 'new' | null>(null);
  const [sourcesProject, setSourcesProject] = useState<Project | null>(null);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [error, setError] = useState('');
  const [conflict, setConflict] = useState<Project | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const attempt = useRef<{ fingerprint: string; key: string } | null>(null);
  const nameField = useRef<HTMLInputElement>(null);
  const locked = useRef(false);
  const previousAccount = useRef<string | null>(null);
  const fence = useRef(new SessionFence());
  const [callbackError] = useState(() => signInError(new URLSearchParams(window.location.search).get('error')));
  useEffect(() => { if (window.location.search) window.history.replaceState(null, '', window.location.pathname); }, []);

  useEffect(() => {
    function revalidate() {
      fence.current.invalidate();
      setSessionLoading(true); setList(null); setSessionTick(v => v + 1);
    }
    function visible() { if (document.visibilityState === 'visible') revalidate(); }
    window.addEventListener('focus', revalidate);
    document.addEventListener('visibilitychange', visible);
    return () => { window.removeEventListener('focus', revalidate); document.removeEventListener('visibilitychange', visible); };
  }, []);

  useEffect(() => {
    const abort = new AbortController();
    const generation = fence.current.invalidate();
    const current = () => !abort.signal.aborted && fence.current.current(generation);
    setSessionLoading(true); setSessionError(''); setList(null);
    void request<Session>('/session', { signal: abort.signal }).then(value => {
      if (!current()) return;
      if (accountChanged(previousAccount.current, value.account.id)) {
        setEditor(null); setName(''); setDescription(''); setError(''); setConflict(null); setNotice(''); attempt.current = null; setPage(1); setArchived(false); setSourcesProject(null);
      }
      previousAccount.current = value.account.id;
      setSession(value);
    }).catch(e => {
      if (!current()) return;
      setSession(null); if (!(e instanceof ApiError && e.status === 401)) setSessionError(e.message);
    }).finally(() => { if (current()) setSessionLoading(false); });
    return () => abort.abort();
  }, [sessionTick]);

  useEffect(() => {
    if (!session || sessionLoading) return;
    const abort = new AbortController();
    const generation = fence.current.ticket();
    const current = () => !abort.signal.aborted && fence.current.current(generation);
    setLoading(true); setListError('');
    void request<ProjectPage>(`/projects?page=${page}&page_size=10&include_archived=${archived}`, { signal: abort.signal }).then(value => {
      if (!current()) return;
      if (typeof value.account_id !== 'string') throw new ApiError(503, 'invalid_response', 'Project identity could not be verified. Refresh or contact the server operator.');
      if (!fence.current.accepts(generation, session.account.id, value.account_id)) {
        fence.current.invalidate(); setList(null); setSessionLoading(true); setSessionTick(v => v + 1); return;
      }
      setList(value);
    }).catch(e => {
      if (!current()) return;
      setListError(e.message); if (e instanceof ApiError && e.status === 401) { setSession(null); setList(null); setSessionError(e.message); }
    }).finally(() => { if (current()) setLoading(false); });
    return () => abort.abort();
  }, [session, sessionLoading, page, archived, tick]);
  useEffect(() => { if (editor) nameField.current?.focus(); }, [editor]);
  function openEditor(project: Project | 'new') {
    setEditor(project); setName(project === 'new' ? '' : project.name); setDescription(project === 'new' ? '' : project.description ?? '');
    setError(''); setConflict(null); attempt.current = null;
  }
  async function mutate(path: string, method: string, body: unknown, success: string) {
    if (!session || sessionLoading || locked.current) return;
    const generation = fence.current.ticket();
    locked.current = true; setBusy(true); setError(''); setConflict(null);
    attempt.current = mutationKey(attempt.current, JSON.stringify({ path, method, body }));
    try {
      await request<Project>(path, { method, body, csrf: session.csrf_token, key: attempt.current.key });
      if (!fence.current.current(generation)) return;
      attempt.current = null; setEditor(null); setSourcesProject(null); setNotice(success); setPage(1); setTick(v => v + 1);
    } catch (e) {
      if (!fence.current.current(generation)) return;
      const failure = e as ApiError; setError(failure.message);
      if (failure.code === 'csrf_failed') { fence.current.invalidate(); setSessionLoading(true); setSessionTick(v => v + 1); }
      if (failure.code === 'version_conflict' && failure.latest) setConflict(failure.latest);
      if (failure.status === 401) { setSession(null); setSessionError(failure.message); }
      // Validation/conflict is a known failure. An uncertain transport failure retains the key.
      if (failure.status > 0 && failure.status < 500) attempt.current = null;
    } finally { locked.current = false; setBusy(false); }
  }
  function save(event: FormEvent) {
    event.preventDefault();
    if (!name.trim()) { setError('Enter a project name.'); nameField.current?.focus(); return; }
    if (!editor) return;
    void mutate(editor === 'new' ? '/projects' : `/projects/${editor.id}`, editor === 'new' ? 'POST' : 'PATCH', { name: name.trim(), description: description.trim(), ...(editor === 'new' ? {} : { expected_version: editor.version }) }, editor === 'new' ? 'Project created.' : 'Project updated.');
  }
  async function logout() {
    if (!session || sessionLoading || locked.current) return;
    const generation = fence.current.ticket();
    locked.current = true; setBusy(true);
    try { await request<void>('/auth/logout', { method: 'POST', csrf: session.csrf_token }); if (!fence.current.current(generation)) return; fence.current.invalidate(); setSession(null); setList(null); setEditor(null); setSourcesProject(null); setNotice('Signed out.'); }
    catch (e) { if (fence.current.current(generation)) setSessionError((e as Error).message); }
    finally { locked.current = false; setBusy(false); }
  }

  const revalidateSession = useCallback(() => { fence.current.invalidate(); setSessionLoading(true); setList(null); setSessionTick(v => v + 1); }, []);

  return <main>
    <header><a className="brand" href="/">SignalDesk<span>SD</span></a><div className="header-actions"><span className="stage">Project workspace</span>{session && !sessionLoading && <><span className="account">{session.account.display_name}</span><button className="secondary" disabled={busy} onClick={() => void logout()}>Sign out</button></>}</div></header>
    <div className="announcement" role="status" aria-live="polite">{!sessionLoading && notice}</div>
    {sessionLoading ? <section className="status-card" role="status">Checking your session…</section> : !session ? <>
      <section className="intro"><p className="eyebrow">From release notes to clear decisions</p><h1>A quieter place<br />to keep up.</h1><p className="lede">Your projects, their dependencies, and the decisions that keep them moving.</p></section>
      <section className="status-card sign-in"><h2>Welcome to SignalDesk</h2><p>Sign in with GitHub to create and manage your private project workspace.</p>{(callbackError || sessionError) && <p className="error" role="alert">{sessionError || callbackError}</p>}{(editor || sourcesProject) && <p className="muted">Your unsaved draft is kept in this tab. Sign-in opens a new tab; return here and retry the session check afterward.</p>}<a className="button" href="/api/v1/auth/github/start" target={editor || sourcesProject ? '_blank' : undefined} rel={editor || sourcesProject ? 'noopener noreferrer' : undefined}>Continue with GitHub <span aria-hidden="true">↗</span></a>{sessionError && <button className="secondary" onClick={() => setSessionTick(v => v + 1)}>Retry session check</button>}<p className="muted">No private repository access is requested. Project access is tied to your signed-in account.</p></section>
    </> : <>
      <section className="intro compact"><p className="eyebrow">Your workspace</p><h1>Projects, in focus.</h1><p className="lede">Keep a home for each application you maintain. Start with a name and a little context.</p></section>
      {sessionError && <p className="error" role="alert">{sessionError}</p>}
      <section aria-labelledby="projects-heading">
        <div className="section-heading"><div><h2 id="projects-heading">Your projects</h2><p className="muted">{list && !loading ? `${list.total} ${archived ? 'total' : 'active'} project${list.total === 1 ? '' : 's'}` : 'Owned by your account'}</p></div><button onClick={() => openEditor('new')} disabled={busy}>+ New project</button></div>
        <label className="checkbox"><input type="checkbox" checked={archived} disabled={busy} onChange={e => { setArchived(e.target.checked); setPage(1); }} />Include archived projects</label>
        {loading ? <div className="status-card" role="status">Loading projects…</div> : listError ? <div className="status-card"><p className="error" role="alert">{listError}</p><button onClick={() => setTick(v => v + 1)}>Retry projects</button></div> : list?.items.length === 0 ? <div className="status-card empty"><span className="empty-icon" aria-hidden="true">◇</span><h3>{page > 1 ? 'No projects on this page' : archived ? 'No projects yet' : 'No active projects yet'}</h3><p>{page > 1 ? 'Return to the first page to refresh your workspace.' : 'Create your first project to organize the applications you maintain.'}</p>{page > 1 ? <button onClick={() => setPage(1)}>First page</button> : <button onClick={() => openEditor('new')}>Create a project</button>}</div> : <div className="project-grid">{list?.items.map(project => <article className="status-card project-card" key={project.id}><div className="project-top"><span className="project-mark" aria-hidden="true">{project.name.charAt(0).toUpperCase()}</span><span className={`badge ${project.archived_at ? 'archived' : ''}`}>{project.archived_at ? 'Archived' : 'Active'}</span></div><h3>{project.name}</h3><p className="project-description">{project.description || 'No description added.'}</p><div className="form-actions"><button disabled={busy} onClick={() => setSourcesProject(project)}>Sources & releases</button><button className="secondary" disabled={busy} onClick={() => openEditor(project)}>Manage project</button></div></article>)}</div>}
        {list && <nav className="pagination" aria-label="Project pages"><button className="secondary" disabled={page <= 1 || loading || busy} onClick={() => setPage(v => v - 1)}>Previous</button><span>Page {page}</span><button className="secondary" disabled={!list.has_next || loading || busy} onClick={() => setPage(v => v + 1)}>Next</button></nav>}
      </section>
      {editor && <section className="status-card editor" aria-labelledby="editor-heading"><div className="section-heading"><h2 id="editor-heading">{editor === 'new' ? 'Create a project' : 'Manage project'}</h2><button className="secondary" disabled={busy} onClick={() => { setEditor(null); setError(''); }}>Close editor</button></div><form onSubmit={save}><label htmlFor="project-name">Project name</label><input ref={nameField} id="project-name" value={name} onChange={e => setName(e.target.value)} required maxLength={100} disabled={busy || (editor !== 'new' && !!editor.archived_at)} placeholder="Booking API" /><label htmlFor="description">Description <span className="muted">(optional)</span></label><textarea id="description" value={description} onChange={e => setDescription(e.target.value)} maxLength={2000} disabled={busy || (editor !== 'new' && !!editor.archived_at)} placeholder="What does this application do?" />{error && <div><p className="error" role="alert">{error}</p><button type="button" className="secondary" onClick={() => setSessionTick(v => v + 1)}>Refresh session</button></div>}{conflict && <div className="conflict"><h3>Latest saved version</h3><p><strong>{conflict.name}</strong> · {conflict.archived_at ? 'Archived' : 'Active'} · Version {conflict.version}</p><p>{conflict.description || 'No description.'}</p><p>Your input above is unchanged. Compare it before saving.</p><button type="button" className="secondary" onClick={() => { setEditor(conflict); setConflict(null); setError('Latest version loaded. Your draft is kept; review and save explicitly.'); }}>Use latest version, keep my draft</button></div>}<div className="form-actions"><button disabled={busy || !!conflict || (editor !== 'new' && !!editor.archived_at)}>{busy ? 'Saving…' : editor === 'new' ? 'Create project' : 'Save changes'}</button>{editor !== 'new' && <button type="button" className="secondary" disabled={busy || !!conflict} onClick={() => void mutate(`/projects/${editor.id}/${editor.archived_at ? 'restore' : 'archive'}`, 'POST', { expected_version: editor.version }, editor.archived_at ? 'Project restored.' : 'Project archived. History is preserved.')}>{editor.archived_at ? 'Restore project' : 'Archive project'}</button>}</div>{editor !== 'new' && <p className="muted">Archiving preserves project history and removes the project from the default list. Restore an archived project before editing. Restoring is subject to the active project limit.</p>}</form></section>}
    </>}
    {sourcesProject && <SourcesPanel key={sourcesProject.id} project={sourcesProject} session={session} enabled={!!session && !sessionLoading} fence={fence.current} onRevalidate={revalidateSession} onClose={() => setSourcesProject(null)} />}
    <aside><strong>What is available today?</strong><p>GitHub sign-in, owned projects, public repository connections, and manual bounded release collection. Scheduled collection and review decisions are coming in later stages.</p></aside><footer>SignalDesk · Portfolio MVP <span>Projects & GitHub releases</span></footer>
  </main>;
}
createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>);
