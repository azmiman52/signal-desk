import type { Release } from './sources-model';
import { displayDate, safeGithubUrl } from './sources-model';

export function ReleaseContent({ release }: { release: Release & { body: string | null } }) {
  const original = safeGithubUrl(release.url);
  return <><h4>{release.title || release.tag_name}</h4><p className="muted">Published {displayDate(release.published_at)} · First captured {displayDate(release.first_seen_at)}</p>{original && <a href={original} target="_blank" rel="noopener noreferrer">Original GitHub release ↗</a>}<pre className="release-notes">{release.body || 'This release has no notes.'}</pre></>;
}
