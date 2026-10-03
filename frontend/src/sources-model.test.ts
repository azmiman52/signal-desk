import { describe, expect, it } from 'vitest';
import { canCollect, safeGithubUrl, sourceStatus, type Subscription } from './sources-model';
import type { Project } from './api';
const project: Project = { id: 'p', name: 'API', description: null, archived_at: null, version: 1, created_at: '2026-10-01T00:00:00Z' };
const source: Subscription = { id: 's', account_id: 'a', project_id: 'p', source: { id: 'r', repository: 'org/repo', url: 'https://github.com/org/repo' }, state: 'active', include_prereleases: false, version: 1, connection_state: 'pending', bootstrap_state: 'pending', last_success_at: null, next_check_at: null, active_run_id: null };
describe('source operating states', () => {
  it('does not confuse saved configuration with successful collection', () => {
    expect(sourceStatus(source)).toBe('Pending first successful check');
    expect(sourceStatus({ ...source, connection_state: 'connected' })).toBe('Connected');
    expect(sourceStatus({ ...source, connection_state: 'attention', last_success_at: '2026-10-01T00:00:00Z' })).toBe('Needs attention');
  });
  it.each(['paused', 'removed'] as const)('does not collect %s sources', state => {
    expect(canCollect(project, { ...source, state }, false, null)).toBe(false);
  });
  it('blocks archived projects, active runs and in-flight requests', () => {
    expect(canCollect({ ...project, archived_at: '2026-10-01' }, source, false, null)).toBe(false);
    expect(canCollect(project, { ...source, active_run_id: 'run' }, false, null)).toBe(false);
    expect(canCollect(project, source, true, null)).toBe(false);
  });
  it('enables manual retry only when the provider deferral has expired', () => {
    const now = Date.parse('2026-10-03T12:00:00Z');
    expect(canCollect(project, source, false, '2026-10-03T12:01:00Z', now)).toBe(false);
    expect(canCollect(project, source, false, '2026-10-03T11:59:00Z', now)).toBe(true);
  });
});
describe('release and source original links', () => {
  it.each(['javascript:alert(1)', 'data:text/html,test', 'https://github.com.evil.test/a', 'https://user:pass@github.com/a', 'http://github.com/a', 'https://github.com:8443/a'])('rejects unsafe destination %s', value => expect(safeGithubUrl(value)).toBeNull());
  it('permits public GitHub HTTPS links', () => expect(safeGithubUrl('https://github.com/org/repo/releases/tag/v1')).toBe('https://github.com/org/repo/releases/tag/v1'));
});
