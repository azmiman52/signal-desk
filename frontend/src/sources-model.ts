import type { Project } from './api';
export interface OwnedPage<T> { account_id: string; items: T[]; page: number; page_size: number; total: number; has_next: boolean; as_of: string }
export interface Subscription { id: string; account_id: string; project_id: string; source: { id: string; repository: string; url: string }; state: 'active' | 'paused' | 'removed'; include_prereleases: boolean; version: number; bootstrap_state: string; connection_state: 'pending' | 'connected' | 'attention'; last_success_at: string | null; next_check_at: string | null; active_run_id: string | null }
export interface CollectionRun { id: string; state: string; attempt_count: number; counts: { discovered: number; created: number; updated: number; skipped: number }; coverage: string; created_at?: string; started_at?: string | null; finished_at?: string | null; retry_at?: string | null; error: { code: string; message: string } | null }
export interface Release { id: string; title: string; tag_name: string; url: string; published_at: string | null; first_seen_at: string; last_seen_at: string; prerelease: boolean }
export function safeGithubUrl(value: string): string | null {
  try { const url = new URL(value); return url.protocol === 'https:' && url.hostname === 'github.com' && !url.username && !url.password && !url.port ? url.href : null; } catch { return null; }
}
export function isSubscription(value: unknown): value is Subscription {
  return !!value && typeof value === 'object' && 'source' in value && 'version' in value && 'id' in value && 'account_id' in value;
}
export function canCollect(project: Project, source: Subscription, pending: boolean, retryAt: string | null, now = Date.now()) {
  return !project.archived_at && source.state === 'active' && !source.active_run_id && !pending && (!retryAt || new Date(retryAt).getTime() <= now);
}
export function sourceStatus(source: Subscription) {
  if (source.state === 'removed') return 'Removed · history retained';
  if (source.state === 'paused') return 'Paused';
  if (source.connection_state === 'attention') return 'Needs attention';
  if (source.active_run_id) return 'Collection in progress';
  return source.connection_state === 'connected' ? 'Connected' : 'Pending first successful check';
}
export function displayDate(value: string | null | undefined) {
  if (!value) return 'Never';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? 'Unavailable' : date.toLocaleString(undefined, { timeZoneName: 'short' });
}
