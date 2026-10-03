import { describe, expect, it, vi } from 'vitest';
import { accountChanged, mutationKey, request, signInError } from './api';

describe('authenticated API contract', () => {
  it('sends CSRF, idempotency and version with same-origin cookies', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({ id: 'p', version: 2 })));
    await request('/projects/p', { method: 'PATCH', csrf: 'token', key: 'uuid', body: { name: 'New', expected_version: 1 } }, fetcher);
    expect(fetcher).toHaveBeenCalledWith('/api/v1/projects/p', expect.objectContaining({ credentials: 'same-origin', cache: 'no-store', method: 'PATCH', headers: expect.objectContaining({ 'X-CSRF-Token': 'token', 'Idempotency-Key': 'uuid' }), body: JSON.stringify({ name: 'New', expected_version: 1 }) }));
  });
  it('preserves latest owned conflict record without displaying raw server messages', async () => {
    const latest = { id: 'p', version: 4, name: 'Changed' };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({ error: { code: 'version_conflict', message: 'raw secret', latest } }), { status: 409 }));
    await expect(request('/projects/p', {}, fetcher)).rejects.toMatchObject({ code: 'version_conflict', latest, message: expect.not.stringContaining('raw secret') });
  });
  it('handles empty logout responses', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 204 }));
    expect(await request('/auth/logout', { method: 'POST', csrf: 'token' }, fetcher)).toBeUndefined();
  });
  it('distinguishes expired sessions from unavailable services', async () => {
    const expired = vi.fn<typeof fetch>().mockResolvedValue(new Response('{}', { status: 401 }));
    await expect(request('/session', {}, expired)).rejects.toMatchObject({ status: 401 });
    const offline = vi.fn<typeof fetch>().mockRejectedValue(new TypeError('offline'));
    await expect(request('/projects', {}, offline)).rejects.toMatchObject({ status: 0, code: 'network_error' });
  });
  it('never echoes arbitrary OAuth callback data', () => {
    expect(signInError('<script>secret</script>')).not.toContain('secret');
    expect(signInError('oauth_not_configured')).toContain('not been configured');
    expect(signInError('oauth_denied')).toContain('canceled');
  });
  it('keeps keys for uncertain retries and changes keys for changed payloads', () => {
    const initial = mutationKey(null, 'payload');
    expect(mutationKey(initial, 'payload')).toEqual(initial);
    expect(mutationKey(initial, 'edited').key).not.toBe(initial.key);
  });
});

describe('session identity and bounded requests', () => {
  it('preserves same-account drafts but detects an account change', () => {
    expect(accountChanged(null, 'first')).toBe(false);
    expect(accountChanged('first', 'first')).toBe(false);
    expect(accountChanged('first', 'second')).toBe(true);
  });
  it('aborts a stalled request and treats the outcome as uncertain', async () => {
    const fetcher: typeof fetch = (_input, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(init.signal?.reason));
    });
    await expect(request('/projects', { method: 'POST', timeoutMs: 10 }, fetcher)).rejects.toMatchObject({ status: 0, code: 'network_error' });
  });
});

describe('provider cooldown metadata', () => {
  it('carries provider retry time without exposing the raw server message', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({ error: { code: 'cooldown', message: 'internal details', retry_at: '2026-10-03T12:01:00Z' } }), { status: 429, headers: { 'Retry-After': '60' } }));
    await expect(request('/subscriptions/s/checks', {}, fetcher)).rejects.toMatchObject({ code: 'cooldown', retryAt: '2026-10-03T12:01:00Z', message: expect.not.stringContaining('internal details') });
  });
});
