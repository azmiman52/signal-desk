import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { ReleaseContent } from './ReleaseContent';
import type { Release } from './sources-model';
const release: Release = { id: 'r', title: '<script>alert(1)</script>', tag_name: 'v1', url: 'javascript:alert(1)', published_at: null, first_seen_at: '2026-10-03T00:00:00Z', last_seen_at: '2026-10-03T00:00:00Z', prerelease: false };
describe('untrusted release content rendering', () => {
  it('renders source notes as escaped plain text without remote images or unsafe links', () => {
    const html = renderToStaticMarkup(<ReleaseContent release={{ ...release, body: '<img src="https://tracker.invalid/pixel" onerror="alert(1)">\n<script>bad()</script>' }} />);
    expect(html).not.toContain('<img'); expect(html).not.toContain('<script>'); expect(html).not.toContain('<a ');
    expect(html).toContain('&lt;img'); expect(html).toContain('&lt;script&gt;');
  });
  it('provides an original GitHub release link with safe new-tab attributes', () => {
    const html = renderToStaticMarkup(<ReleaseContent release={{ ...release, url: 'https://github.com/org/repo/releases/tag/v1', body: '' }} />);
    expect(html).toContain('rel="noopener noreferrer"'); expect(html).toContain('This release has no notes.');
  });
});
