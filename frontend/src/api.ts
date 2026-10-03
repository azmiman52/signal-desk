export interface Account { id: string; display_name: string; kind: string; timezone: string }
export interface Session { account: Account; csrf_token: string; expires_at: string }
export interface Project { id: string; name: string; description: string | null; archived_at: string | null; version: number; created_at: string; active_source_count?: number }
export interface ProjectPage { account_id: string; items: Project[]; page: number; page_size: number; total: number; has_next: boolean; as_of: string }
export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public latest?: Project) { super(message); }
}
const messages: Record<string, string> = {
  version_conflict: 'This project changed elsewhere. Your draft is preserved. Review the latest version before saving again.',
  capacity_exceeded: 'The active project limit has been reached. Archive a project before creating or restoring another.',
  csrf_failed: 'Your security token is no longer valid. Refresh your session before trying again.',
  not_found: 'This project is no longer available to your account.',
  validation_error: 'Check your project name and description, then try again.',
  idempotency_key_reused: 'This request key was already used with different details. Review your draft and submit again.',
  service_unavailable: 'The service is temporarily unavailable. Your draft is preserved.',
  rate_limited: 'Too many requests. Wait a moment before trying again.',
  invalid_transition: 'This project cannot perform that action in its current state.',
};
export async function request<T>(path: string, options: { method?: string; body?: unknown; csrf?: string; key?: string; signal?: AbortSignal; timeoutMs?: number } = {}, fetcher: typeof fetch = fetch): Promise<T> {
  let response: Response;
  try {
    response = await fetcher(`/api/v1${path}`, {
      method: options.method ?? 'GET', credentials: 'same-origin', cache: 'no-store', signal: options.signal ? AbortSignal.any([options.signal, AbortSignal.timeout(options.timeoutMs ?? 15000)]) : AbortSignal.timeout(options.timeoutMs ?? 15000),
      headers: { Accept: 'application/json', ...(options.body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...(options.csrf ? { 'X-CSRF-Token': options.csrf } : {}), ...(options.key ? { 'Idempotency-Key': options.key } : {}) },
      ...(options.body !== undefined ? { body: JSON.stringify(options.body) } : {}),
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ApiError(0, 'network_error', 'Cannot reach SignalDesk. Your draft is preserved; retry when the connection returns.');
  }
  if (response.status === 204) return undefined as T;
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const code = typeof body?.error?.code === 'string' ? body.error.code : 'unknown';
    throw new ApiError(response.status, code, response.status === 401 ? 'Your session has ended. Sign in again to continue.' : messages[code] ?? 'The request could not be completed. Try again shortly.', body?.error?.latest);
  }
  if (!body) throw new ApiError(response.status, 'invalid_response', 'The server returned an unexpected response. Please try again.');
  return body as T;
}
export function signInError(code: string | null): string | null {
  if (!code) return null;
  if (code === 'oauth_not_configured') return 'GitHub sign-in has not been configured on this server. The operator needs to configure the GitHub OAuth client and callback URL.';
  if (['access_denied', 'oauth_denied'].includes(code)) return 'GitHub sign-in was canceled. You can try again when ready.';
  return 'GitHub sign-in could not be completed. Please start again.';
}
// A retry after an uncertain network failure uses the same key; edits get a new key.
export function mutationKey(previous: { fingerprint: string; key: string } | null, fingerprint: string) {
  return previous?.fingerprint === fingerprint ? previous : { fingerprint, key: crypto.randomUUID() };
}

export function accountChanged(previousId: string | null, nextId: string) { return previousId !== null && previousId !== nextId; }
