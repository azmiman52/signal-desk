import { describe, expect, it } from 'vitest';
import { SessionFence } from './session-fence';

describe('cross-tab session response ordering', () => {
  it('rejects B-cookie projects while the verified header belongs to A', () => {
    const fence = new SessionFence();
    const request = fence.ticket();
    expect(fence.accepts(request, 'account-A', 'account-B')).toBe(false);
    expect(fence.accepts(request, 'account-A', 'account-A')).toBe(true);
  });
  it('rejects late session/list/mutation completions after focus invalidation', () => {
    const fence = new SessionFence();
    const oldSession = fence.invalidate();
    const oldList = fence.ticket();
    const oldMutation = fence.ticket();
    fence.invalidate(); // focus event invalidates before starting the next session fetch
    expect(fence.current(oldSession)).toBe(false);
    expect(fence.accepts(oldList, 'A', 'A')).toBe(false);
    expect(fence.current(oldMutation)).toBe(false);
    const newSession = fence.invalidate();
    expect(fence.accepts(newSession, 'B', 'B')).toBe(true);
  });
  it('rejects older responses even when the same account logs in again', () => {
    const fence = new SessionFence();
    const old = fence.ticket();
    fence.invalidate();
    expect(fence.accepts(old, 'A', 'A')).toBe(false);
  });
});
